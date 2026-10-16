import os
import math
import requests
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from PIL import Image

def get_torch_device(preferred: str = "auto") -> torch.device:
    if preferred != "auto":
        return torch.device(preferred)
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")

# ==================== RRDBNet Architecture ====================

def default_init_weights(module_list, scale=1, bias_fill=0, **kwargs):
    if not isinstance(module_list, list):
        module_list = [module_list]
    for module in module_list:
        for m in module.modules():
            if isinstance(m, (nn.Conv2d, nn.Linear)):
                nn.init.kaiming_normal_(m.weight, **kwargs)
                m.weight.data *= scale
                if m.bias is not None:
                    m.bias.data.fill_(bias_fill)

class ResidualDenseBlock(nn.Module):
    def __init__(self, num_feat=64, num_grow_ch=32):
        super().__init__()
        self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
        self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)
        default_init_weights([self.conv1, self.conv2, self.conv3, self.conv4, self.conv5], 0.1)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x

class RRDB(nn.Module):
    def __init__(self, num_feat=64, num_grow_ch=32):
        super().__init__()
        self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

    def forward(self, x):
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        return out * 0.2 + x

def pixel_unshuffle(x, scale):
    b, c, hh, hw = x.size()
    out_channel = c * (scale ** 2)
    h = hh // scale
    w = hw // scale
    x_view = x.view(b, c, h, scale, w, scale)
    return x_view.permute(0, 1, 3, 5, 2, 4).reshape(b, out_channel, h, w)

class RRDBNet(nn.Module):
    def __init__(self, num_in_ch=3, num_out_ch=3, scale=4, num_feat=64, num_block=23, num_grow_ch=32):
        super().__init__()
        self.scale = scale
        if scale == 2:
            num_in_ch = num_in_ch * 4
        elif scale == 1:
            num_in_ch = num_in_ch * 16
        self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
        self.body = nn.Sequential(*[RRDB(num_feat, num_grow_ch) for _ in range(num_block)])
        self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        if scale == 8:
            self.conv_up3 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        if self.scale == 2:
            feat = pixel_unshuffle(x, scale=2)
        elif self.scale == 1:
            feat = pixel_unshuffle(x, scale=4)
        else:
            feat = x
        feat = self.conv_first(feat)
        body_feat = self.conv_body(self.body(feat))
        feat = feat + body_feat
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode='nearest')))
        feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode='nearest')))
        if self.scale == 8:
            feat = self.lrelu(self.conv_up3(F.interpolate(feat, scale_factor=2, mode='nearest')))
        out = self.conv_last(self.lrelu(self.conv_hr(feat)))
        return out

# ==================== Patch & Stitch Utilities ====================

def pad_reflect(image, pad_size):
    h, w, c = image.shape
    new_img = np.zeros([h + pad_size * 2, w + pad_size * 2, c], dtype=np.uint8)
    new_img[pad_size:-pad_size, pad_size:-pad_size, :] = image
    new_img[0:pad_size, pad_size:-pad_size, :] = np.flip(image[0:pad_size, :, :], axis=0)
    new_img[-pad_size:, pad_size:-pad_size, :] = np.flip(image[-pad_size:, :, :], axis=0)
    new_img[:, 0:pad_size, :] = np.flip(new_img[:, pad_size:pad_size*2, :], axis=1)
    new_img[:, -pad_size:, :] = np.flip(new_img[:, -pad_size*2:-pad_size, :], axis=1)
    return new_img

def unpad_image(image, pad_size):
    return image[pad_size:-pad_size, pad_size:-pad_size, :]

def split_into_patches(image_array, patch_size=192, padding_size=24):
    xmax, ymax, _ = image_array.shape
    x_remainder = xmax % patch_size
    y_remainder = ymax % patch_size
    x_extend = (patch_size - x_remainder) % patch_size
    y_extend = (patch_size - y_remainder) % patch_size
    extended_image = np.pad(image_array, ((0, x_extend), (0, y_extend), (0, 0)), 'edge')
    padded_image = np.pad(extended_image, ((padding_size, padding_size), (padding_size, padding_size), (0, 0)), 'edge')
    
    xmax, ymax, _ = padded_image.shape
    patches = []
    x_lefts = range(padding_size, xmax - padding_size, patch_size)
    y_tops = range(padding_size, ymax - padding_size, patch_size)
    for x in x_lefts:
        for y in y_tops:
            patches.append(padded_image[x - padding_size:x + patch_size + padding_size, y - padding_size:y + patch_size + padding_size, :])
    return np.array(patches), padded_image.shape

def stitch_patches(patches, padded_image_shape, target_shape, padding_size=48):
    xmax, ymax, _ = padded_image_shape
    patches = patches[:, padding_size:-padding_size, padding_size:-padding_size, :]
    patch_size = patches.shape[1]
    n_patches_per_row = ymax // patch_size
    complete_image = np.zeros((xmax, ymax, 3), dtype=np.float32)
    row = -1
    col = 0
    for i in range(len(patches)):
        if i % n_patches_per_row == 0:
            row += 1
            col = 0
        complete_image[row * patch_size:(row + 1) * patch_size, col * patch_size:(col + 1) * patch_size, :] = patches[i]
        col += 1
    return complete_image[0:target_shape[0], 0:target_shape[1], :]

# ==================== Real-ESRGAN Model Engine ====================

HF_URLS = {
    2: "https://huggingface.co/sberbank-ai/Real-ESRGAN/resolve/main/RealESRGAN_x2.pth",
    4: "https://huggingface.co/sberbank-ai/Real-ESRGAN/resolve/main/RealESRGAN_x4.pth",
}

class RealESRGANUpscaler:
    def __init__(self, scale: int = 2, device_pref: str = "auto", weights_dir: str = "weights"):
        self.scale = scale
        self.device = get_torch_device(device_pref)
        self.weights_dir = weights_dir
        os.makedirs(self.weights_dir, exist_ok=True)
        self.model = None

    def _download_weights(self, weights_path: str):
        url = HF_URLS.get(self.scale)
        if not url:
            raise ValueError(f"No pretrained weights available for scale {self.scale}")
        print(f"Downloading Real-ESRGAN x{self.scale} weights to {weights_path}...")
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(weights_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        print("Model weights successfully downloaded.")

    def load(self):
        if self.model is not None:
            return
        weights_path = os.path.join(self.weights_dir, f"RealESRGAN_x{self.scale}.pth")
        if not os.path.exists(weights_path) or os.path.getsize(weights_path) < 1000000:
            self._download_weights(weights_path)

        print(f"Loading Real-ESRGAN x{self.scale} model onto device '{self.device}'...")
        net = RRDBNet(num_in_ch=3, num_out_ch=3, scale=self.scale, num_feat=64, num_block=23, num_grow_ch=32)
        loadnet = torch.load(weights_path, map_location=self.device, weights_only=False)
        if "params" in loadnet:
            net.load_state_dict(loadnet["params"], strict=True)
        elif "params_ema" in loadnet:
            net.load_state_dict(loadnet["params_ema"], strict=True)
        else:
            net.load_state_dict(loadnet, strict=True)

        net.eval()
        net.to(self.device)
        self.model = net

    def predict(self, lr_image: Image.Image, batch_size: int = 4, patch_size: int = 192, padding: int = 24, pad_size: int = 15) -> Image.Image:
        self.load()

        # Check for transparency / alpha channel
        has_alpha = (lr_image.mode in ("RGBA", "LA") or (lr_image.mode == "P" and "transparency" in lr_image.info))
        alpha_channel = lr_image.convert("RGBA").split()[3] if has_alpha else None

        lr_np = np.array(lr_image.convert("RGB"))
        lr_padded = pad_reflect(lr_np, pad_size)
        patches, p_shape = split_into_patches(lr_padded, patch_size=patch_size, padding_size=padding)
        
        img_tensor = torch.FloatTensor(patches / 255.0).permute((0, 3, 1, 2)).to(self.device).detach()

        with torch.no_grad():
            res_list = []
            for i in range(0, img_tensor.shape[0], batch_size):
                batch = img_tensor[i:i + batch_size]
                out = self.model(batch)
                res_list.append(out.cpu())
            res = torch.cat(res_list, dim=0)

        sr_image = res.permute((0, 2, 3, 1)).clamp_(0, 1).numpy()
        padded_size_scaled = tuple(np.multiply(p_shape[0:2], self.scale)) + (3,)
        scaled_image_shape = tuple(np.multiply(lr_padded.shape[0:2], self.scale)) + (3,)
        
        stitched = stitch_patches(
            sr_image,
            padded_image_shape=padded_size_scaled,
            target_shape=scaled_image_shape,
            padding_size=padding * self.scale
        )
        sr_u8 = (stitched * 255.0).clip(0, 255).astype(np.uint8)
        unpadded = unpad_image(sr_u8, pad_size * self.scale)
        sr_img = Image.fromarray(unpadded)

        # Reattach scaled alpha channel to preserve transparent corners
        if alpha_channel is not None:
            scaled_alpha = alpha_channel.resize(sr_img.size, Image.Resampling.BILINEAR)
            sr_img.putalpha(scaled_alpha)

        return sr_img
