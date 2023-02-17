"""
wild mixture of
https://github.com/lucidrains/denoising-diffusion-pytorch/blob/7706bdfc6f527f58d33f84b7b522e61e6e3164b3/denoising_diffusion_pytorch/denoising_diffusion_pytorch.py
https://github.com/openai/improved-diffusion/blob/e94489283bb876ac1477d5dd7709bbbd2d9902ce/improved_diffusion/gaussian_diffusion.py
https://github.com/CompVis/taming-transformers
-- merci
"""

import torch
import torch.nn as nn
import numpy as np
import pytorch_lightning as pl
from torch.optim.lr_scheduler import LambdaLR
from einops import rearrange, repeat
from contextlib import contextmanager
from functools import partial
from tqdm import tqdm
from torchvision.utils import make_grid
from pytorch_lightning.utilities.distributed import rank_zero_only
import torchmetrics
from taming.modules.diffusionmodules.ddpm import DDPM
from taming.modules.metrics.metrics import CodebookUsageMetric, FIDMetric
from taming.util import log_txt_as_img, exists, default, ismap, isimage, mean_flat, count_params, instantiate_from_config
from taming.modules.ema import LitEma
from taming.models.vqgan import VQModelInterface
from taming.modules.diffusionmodules.util import make_beta_schedule, extract_into_tensor, noise_like
from taming.modules.diffusionmodules.ddim import DDIMSampler


class VQDiffusion(DDPM):
    """main class"""
    def __init__(self,
                 encoder_config,
                 num_timesteps_cond=None,
                 concat_mode=True,
                 cond_stage_forward=None,
                 conditioning_key=None,
                 scale_factor=1.0,
                 scale_by_std=False,
                 *args, **kwargs):
        self.num_timesteps_cond = default(num_timesteps_cond, 1)
        self.scale_by_std = scale_by_std
        assert self.num_timesteps_cond <= kwargs['timesteps']
        # for backwards compatibility after implementation of DiffusionWrapper
        if conditioning_key is None:
            conditioning_key = 'cat_init'
        ckpt_path = kwargs.pop("ckpt_path", None)
        ignore_keys = kwargs.pop("ignore_keys", [])
        super().__init__(conditioning_key=conditioning_key, *args, **kwargs)
        self.concat_mode = concat_mode
        try:
            self.num_downs = len(encoder_config.params.ddconfig.ch_mult) - 1
        except:
            self.num_downs = 0
        if not scale_by_std:
            self.scale_factor = scale_factor
        else:
            self.register_buffer('scale_factor', torch.tensor(scale_factor))

        self.encoder = instantiate_from_config(encoder_config)
        self.cond_stage_forward = cond_stage_forward
        self.clip_denoised = False
        self.bbox_tokenizer = None

        self.metrics_dict = torch.nn.ModuleDict({"PSNR":torchmetrics.PeakSignalNoiseRatio(data_range=1.0),
                             "FID":FIDMetric(),
                             #"Inception":InceptionMetric()
                            "CodebookUsage":CodebookUsageMetric(encoder_config['params']['n_embed']),
                            })
        self.restarted_from_ckpt = False
        if ckpt_path is not None:
            self.init_from_ckpt(ckpt_path, ignore_keys)
            self.restarted_from_ckpt = True

    def make_cond_schedule(self, ):
        self.cond_ids = torch.full(size=(self.num_timesteps,), fill_value=self.num_timesteps - 1, dtype=torch.long)
        ids = torch.round(torch.linspace(0, self.num_timesteps - 1, self.num_timesteps_cond)).long()
        self.cond_ids[:self.num_timesteps_cond] = ids

    # @rank_zero_only
    # @torch.no_grad()
    # def on_train_batch_start(self, batch, batch_idx, dataloader_idx):
    #     # only for very first batch
    #     if self.scale_by_std and self.current_epoch == 0 and self.global_step == 0 and batch_idx == 0 and not self.restarted_from_ckpt:
    #         assert self.scale_factor == 1., 'rather not use custom rescaling and std-rescaling simultaneously'
    #         # set rescale weight to 1./std of encodings
    #         print("### USING STD-RESCALING ###")
    #         x = super().get_input(batch, self.first_stage_key)
    #         x = x.to(self.device)
    #         encoder_posterior = self.encode_first_stage(x)
    #         z = self.get_first_stage_encoding(encoder_posterior).detach()
    #         del self.scale_factor
    #         self.register_buffer('scale_factor', 1. / z.flatten().std())
    #         print(f"setting self.scale_factor to {self.scale_factor}")
    #         print("### USING STD-RESCALING ###")

    def register_schedule(self,
                          given_betas=None, beta_schedule="linear", timesteps=1000,
                          linear_start=1e-4, linear_end=2e-2, cosine_s=8e-3):
        super().register_schedule(given_betas, beta_schedule, timesteps, linear_start, linear_end, cosine_s)

        self.shorten_cond_schedule = self.num_timesteps_cond > 1
        if self.shorten_cond_schedule:
            self.make_cond_schedule()

    def _get_denoise_row_from_list(self, samples, desc='', force_no_decoder_quantization=False):
        denoise_row = []
        for zd in tqdm(samples, desc=desc):
            denoise_row.append(self.decode_first_stage(zd.to(self.device),
                                                            force_not_quantize=force_no_decoder_quantization))
        n_imgs_per_row = len(denoise_row)
        denoise_row = torch.stack(denoise_row)  # n_log_step, n_row, C, H, W
        denoise_grid = rearrange(denoise_row, 'n b c h w -> b n c h w')
        denoise_grid = rearrange(denoise_grid, 'b n c h w -> (b n) c h w')
        denoise_grid = make_grid(denoise_grid, nrow=n_imgs_per_row)
        return denoise_grid


    def get_input(self, batch):
        x =batch['image']
        x = rearrange(x, 'b h w c -> b c h w')
        x = x.to(memory_format=torch.contiguous_format).float()
        c = self.encoder(x)
        return x, c

    def shared_step(self, batch, **kwargs):
        x, c = self.get_input(batch)
        loss = self(x, c)
        return loss

    def training_step(self, batch, batch_idx):
        loss, loss_dict = self.shared_step(batch)

        self.log_dict(loss_dict, prog_bar=True,
                      logger=True, on_step=True, on_epoch=True)

        self.log("global_step", self.global_step,
                 prog_bar=True, logger=True, on_step=True, on_epoch=False)

        if self.use_scheduler:
            lr = self.optimizers().param_groups[0]['lr']
            self.log('lr_abs', lr, prog_bar=True, logger=True, on_step=True, on_epoch=False)

        return loss
    def normalize(self,x):
        return (x.clamp(-1,1)+1)/2
    def validation_step(self, batch, batch_idx):
        x, c = self.get_input(batch)

        loss, loss_dict = self(x, c)

        self.log_dict(loss_dict,prog_bar=False, logger=True, sync_dist=False, on_step=True, on_epoch=False)
        self.log("val/total_loss", loss,
                 prog_bar=False, logger=True, sync_dist=False, on_step=True, on_epoch=False)

        samples, _ = self.sample_log(cond=c[0],batch_size=x.shape[0],ddim=True, ddim_steps=self.ddim_timesteps)
        samples = self.normalize(samples)

        tokens = c[-1][2]
        x = self.normalize(x)

        img = torch.cat([samples,x],dim=-2)
        img = (img.cpu().numpy()*255).astype(np.uint8)
        img = np.moveaxis(img, 1, -1)
        self.logger.experiment.add_images('val_images', img, self.global_step, dataformats='NHWC')
        # self.logger.log_metrics({'val_images':wandb.Image(img)},self.global_step)

        for key_i, metric_i in self.metrics_dict.items():
            if  isinstance(metric_i,CodebookUsageMetric):
                metric_i.update(tokens)
            else:
                metric_i.update(samples,x)
            self.log('val_%s' % (key_i), metric_i,on_epoch=True,
                     prog_bar=True, add_dataloader_idx=False, sync_dist=True)

        return self.log_dict

    def forward(self, x, c, *args, **kwargs):
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), device=self.device).long()
        return self.p_losses(x, c, t, *args, **kwargs)

    def p_losses(self, x_start, cond, t, noise=None):
        noise = default(noise, lambda: torch.randn_like(x_start))
        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        model_output = self.model(x_noisy, t, cond)

        loss_dict = {}
        prefix = 'train' if self.training else 'val'

        loss_simple = self.get_loss(model_output, noise, mean=False).mean([1, 2, 3])
        loss_dict.update({f'{prefix}/loss_simple': loss_simple.mean()})

        loss = self.l_simple_weight * loss_simple.mean()

        # loss_vlb = self.get_loss(model_output, target, mean=False).mean(dim=(1, 2, 3))
        # loss_vlb = (self.lvlb_weights[t] * loss_vlb).mean()
        # loss_dict.update({f'{prefix}/loss_vlb': loss_vlb})
        # loss += (self.original_elbo_weight * loss_vlb)
        # loss_dict.update({f'{prefix}/loss': loss})

        loss += cond[1]
        loss_dict.update({f'{prefix}/embedding_loss': cond[1]})
        return loss, loss_dict

    @torch.no_grad()
    def progressive_denoising(self, cond, shape, verbose=True, callback=None, quantize_denoised=False,
                              img_callback=None, mask=None, x0=None, temperature=1., noise_dropout=0.,
                              score_corrector=None, corrector_kwargs=None, batch_size=None, x_T=None, start_T=None,
                              log_every_t=None):
        if not log_every_t:
            log_every_t = self.log_every_t
        timesteps = self.num_timesteps
        if batch_size is not None:
            b = batch_size if batch_size is not None else shape[0]
            shape = [batch_size] + list(shape)
        else:
            b = batch_size = shape[0]
        if x_T is None:
            img = torch.randn(shape, device=self.device)
        else:
            img = x_T
        intermediates = []
        if cond is not None:
            if isinstance(cond, dict):
                cond = {key: cond[key][:batch_size] if not isinstance(cond[key], list) else
                list(map(lambda x: x[:batch_size], cond[key])) for key in cond}
            else:
                cond = [c[:batch_size] for c in cond] if isinstance(cond, list) else cond[:batch_size]

        if start_T is not None:
            timesteps = min(timesteps, start_T)
        iterator = tqdm(reversed(range(0, timesteps)), desc='Progressive Generation',
                        total=timesteps) if verbose else reversed(
            range(0, timesteps))
        if type(temperature) == float:
            temperature = [temperature] * timesteps

        for i in iterator:
            ts = torch.full((b,), i, device=self.device, dtype=torch.long)
            if self.shorten_cond_schedule:
                assert self.model.conditioning_key != 'hybrid'
                tc = self.cond_ids[ts].to(cond.device)
                cond = self.q_sample(x_start=cond, t=tc, noise=torch.randn_like(cond))

            img, x0_partial = self.p_sample(img, cond, ts,
                                            clip_denoised=self.clip_denoised,
                                            quantize_denoised=quantize_denoised, return_x0=True,
                                            temperature=temperature[i], noise_dropout=noise_dropout,
                                            score_corrector=score_corrector, corrector_kwargs=corrector_kwargs)
            if mask is not None:
                assert x0 is not None
                img_orig = self.q_sample(x0, ts)
                img = img_orig * mask + (1. - mask) * img

            if i % log_every_t == 0 or i == timesteps - 1:
                intermediates.append(x0_partial)
            if callback: callback(i)
            if img_callback: img_callback(img, i)
        return img, intermediates

    @torch.no_grad()
    def p_sample_loop(self, cond, shape, return_intermediates=False,
                      x_T=None, verbose=True, callback=None, timesteps=None, quantize_denoised=False,
                      mask=None, x0=None, img_callback=None, start_T=None,
                      log_every_t=None):

        if not log_every_t:
            log_every_t = self.log_every_t
        device = self.betas.device
        b = shape[0]
        if x_T is None:
            img = torch.randn(shape, device=device)
        else:
            img = x_T

        intermediates = [img]
        if timesteps is None:
            timesteps = self.num_timesteps

        if start_T is not None:
            timesteps = min(timesteps, start_T)
        iterator = tqdm(reversed(range(0, timesteps)), desc='Sampling t', total=timesteps) if verbose else reversed(
            range(0, timesteps))

        if mask is not None:
            assert x0 is not None
            assert x0.shape[2:3] == mask.shape[2:3]  # spatial size has to match

        for i in iterator:
            ts = torch.full((b,), i, device=device, dtype=torch.long)
            if self.shorten_cond_schedule:
                assert self.model.conditioning_key != 'hybrid'
                tc = self.cond_ids[ts].to(cond.device)
                cond = self.q_sample(x_start=cond, t=tc, noise=torch.randn_like(cond))

            img = self.p_sample(img, cond, ts,
                                clip_denoised=self.clip_denoised,
                                quantize_denoised=quantize_denoised)
            if mask is not None:
                img_orig = self.q_sample(x0, ts)
                img = img_orig * mask + (1. - mask) * img

            if i % log_every_t == 0 or i == timesteps - 1:
                intermediates.append(img)
            if callback: callback(i)
            if img_callback: img_callback(img, i)

        if return_intermediates:
            return img, intermediates
        return img

    @torch.no_grad()
    def sample(self, cond, batch_size=16, return_intermediates=False, x_T=None,
               verbose=True, timesteps=None, quantize_denoised=False,
               mask=None, x0=None, shape=None,**kwargs):
        if shape is None:
            shape = (batch_size, self.channels, self.image_size, self.image_size)
        if cond is not None:
            if isinstance(cond, dict):
                cond = {key: cond[key][:batch_size] if not isinstance(cond[key], list) else
                list(map(lambda x: x[:batch_size], cond[key])) for key in cond}
            else:
                cond = [c[:batch_size] for c in cond] if isinstance(cond, list) else cond[:batch_size]
        return self.p_sample_loop(cond,
                                  shape,
                                  return_intermediates=return_intermediates, x_T=x_T,
                                  verbose=verbose, timesteps=timesteps, quantize_denoised=quantize_denoised,
                                  mask=mask, x0=x0)

    @torch.no_grad()
    def sample_log(self,cond,batch_size,ddim, ddim_steps,**kwargs):

        if ddim:
            ddim_sampler = DDIMSampler(self)
            shape = (self.channels, self.image_size, self.image_size)
            samples, intermediates =ddim_sampler.sample(ddim_steps,batch_size,
                                                        shape,cond,verbose=False,**kwargs)

        else:
            samples, intermediates = self.sample(cond=cond, batch_size=batch_size,
                                                 return_intermediates=True,**kwargs)

        return samples, intermediates

    # @torch.no_grad()
    # def log_images(self, batch, N=8, n_row=4, sample=True, ddim_steps=200, ddim_eta=1., return_keys=None,
    #                quantize_denoised=True, inpaint=True, plot_denoise_rows=False, plot_progressive_rows=True,
    #                plot_diffusion_rows=True, **kwargs):
    #
    #     use_ddim = ddim_steps is not None
    #
    #     log = dict()
    #     z, c, x, xrec, xc = self.get_input(batch, self.first_stage_key,
    #                                        return_first_stage_outputs=True,
    #                                        force_c_encode=True,
    #                                        return_original_cond=True,
    #                                        bs=N)
    #     N = min(x.shape[0], N)
    #     n_row = min(x.shape[0], n_row)
    #     log["inputs"] = x
    #     log["reconstruction"] = xrec
    #     if self.model.conditioning_key is not None:
    #         if hasattr(self.cond_stage_model, "decode"):
    #             xc = self.cond_stage_model.decode(c)
    #             log["conditioning"] = xc
    #         elif self.cond_stage_key in ["caption"]:
    #             xc = log_txt_as_img((x.shape[2], x.shape[3]), batch["caption"])
    #             log["conditioning"] = xc
    #         elif self.cond_stage_key == 'class_label':
    #             xc = log_txt_as_img((x.shape[2], x.shape[3]), batch["human_label"])
    #             log['conditioning'] = xc
    #
    #     if plot_diffusion_rows:
    #         # get diffusion row
    #         diffusion_row = list()
    #         z_start = z[:n_row]
    #         for t in range(self.num_timesteps):
    #             if t % self.log_every_t == 0 or t == self.num_timesteps - 1:
    #                 t = repeat(torch.tensor([t]), '1 -> b', b=n_row)
    #                 t = t.to(self.device).long()
    #                 noise = torch.randn_like(z_start)
    #                 z_noisy = self.q_sample(x_start=z_start, t=t, noise=noise)
    #                 diffusion_row.append(self.decode_first_stage(z_noisy))
    #
    #         diffusion_row = torch.stack(diffusion_row)  # n_log_step, n_row, C, H, W
    #         diffusion_grid = rearrange(diffusion_row, 'n b c h w -> b n c h w')
    #         diffusion_grid = rearrange(diffusion_grid, 'b n c h w -> (b n) c h w')
    #         diffusion_grid = make_grid(diffusion_grid, nrow=diffusion_row.shape[0])
    #         log["diffusion_row"] = diffusion_grid
    #
    #     if sample:
    #         # get denoise row
    #         with self.ema_scope("Plotting"):
    #             samples, z_denoise_row = self.sample_log(cond=c,batch_size=N,ddim=use_ddim,
    #                                                      ddim_steps=ddim_steps,eta=ddim_eta)
    #             # samples, z_denoise_row = self.sample(cond=c, batch_size=N, return_intermediates=True)
    #         x_samples = self.decode_first_stage(samples)
    #         log["samples"] = x_samples
    #         if plot_denoise_rows:
    #             denoise_grid = self._get_denoise_row_from_list(z_denoise_row)
    #             log["denoise_row"] = denoise_grid
    #
    #         if quantize_denoised:
    #             # also display when quantizing x0 while sampling
    #             with self.ema_scope("Plotting Quantized Denoised"):
    #                 samples, z_denoise_row = self.sample_log(cond=c,batch_size=N,ddim=use_ddim,
    #                                                          ddim_steps=ddim_steps,eta=ddim_eta,
    #                                                          quantize_denoised=True)
    #                 # samples, z_denoise_row = self.sample(cond=c, batch_size=N, return_intermediates=True,
    #                 #                                      quantize_denoised=True)
    #             x_samples = self.decode_first_stage(samples.to(self.device))
    #             log["samples_x0_quantized"] = x_samples
    #
    #     if plot_progressive_rows:
    #         with self.ema_scope("Plotting Progressives"):
    #             img, progressives = self.progressive_denoising(c,
    #                                                            shape=(self.channels, self.image_size, self.image_size),
    #                                                            batch_size=N)
    #         prog_row = self._get_denoise_row_from_list(progressives, desc="Progressive Generation")
    #         log["progressive_row"] = prog_row
    #
    #     if return_keys:
    #         if np.intersect1d(list(log.keys()), return_keys).shape[0] == 0:
    #             return log
    #         else:
    #             return {key: log[key] for key in return_keys}
    #     return log

    def configure_optimizers(self):
        lr = self.learning_rate

        params = list(self.model.parameters())+list(self.encoder.parameters())
        print("Num parameters in optimiser:", sum([i.numel() for i in params]))

        # if self.learn_logvar:
        #     print('Diffusion model optimizing logvar')
        #     params.append(self.logvar)
        opt = torch.optim.AdamW(params, lr=lr)
        if self.use_scheduler:
            assert 'target' in self.scheduler_config
            scheduler = instantiate_from_config(self.scheduler_config)

            print("Setting up LambdaLR scheduler...")
            scheduler = [
                {
                    'scheduler': LambdaLR(opt, lr_lambda=scheduler.schedule),
                    'interval': 'step',
                    'frequency': 1
                }]
            return [opt], scheduler
        return opt