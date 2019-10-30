"""
Author: Davy Neven
Licensed under the CC BY-NC 4.0 license (https://creativecommons.org/licenses/by-nc/4.0/)
"""
import collections
import os
import threading

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image

import torch

from loss.SpatialEmbLoss import calculate_iou
from util import get_best_overlap


class AverageMeter(object):
  def __init__(self, num_classes=1):
    self.num_classes = num_classes
    self.reset()
    self.lock = threading.Lock()

  def reset(self):
    self.sum = [0] * self.num_classes
    self.count = [0] * self.num_classes
    self.avg_per_class = [0] * self.num_classes
    self.avg = 0

  def update(self, val, cl=0):
    with self.lock:
      self.sum[cl] += val
      self.count[cl] += 1
      self.avg_per_class = [
        x / y if x > 0 else 0 for x, y in zip(self.sum, self.count)]
      self.avg = sum(self.avg_per_class) / len(self.avg_per_class)


class Visualizer:
  def __init__(self, keys):
    self.wins = {k: None for k in keys}

  def display(self, image, key):

    n_images = len(image) if isinstance(image, (list, tuple)) else 1

    if self.wins[key] is None:
      self.wins[key] = plt.subplots(ncols=n_images)

    fig, ax = self.wins[key]
    n_axes = len(ax) if isinstance(ax, collections.Iterable) else 1

    assert n_images == n_axes

    if n_images == 1:
      ax.cla()
      ax.set_axis_off()
      ax.imshow(self.prepare_img(image))
    else:
      for i in range(n_images):
        ax[i].cla()
        ax[i].set_axis_off()
        ax[i].imshow(self.prepare_img(image[i]))

    plt.draw()
    self.mypause(0.001)

  def savePlt(self, image, key, path):

    n_images = len(image) if isinstance(image, (list, tuple)) else 1

    if self.wins[key] is None:
      self.wins[key] = plt.subplots(ncols=n_images)

    fig, ax = self.wins[key]
    n_axes = len(ax) if isinstance(ax, collections.Iterable) else 1

    assert n_images == n_axes

    if n_images == 1:
      ax.cla()
      ax.set_axis_off()
      ax.imshow(self.prepare_img(image))
    else:
      for i in range(n_images):
        ax[i].cla()
        ax[i].set_axis_off()
        ax[i].imshow(self.prepare_img(image[i]))
    # plt.draw()
    plt.savefig(path)

  @staticmethod
  def prepare_img(image):
    if isinstance(image, Image.Image):
      return image

    if isinstance(image, torch.Tensor):
      image.squeeze_()
      image = image.numpy()

    if isinstance(image, np.ndarray):
      if image.ndim == 3 and image.shape[0] in {1, 3}:
        image = image.transpose(1, 2, 0)
      return image

  @staticmethod
  def mypause(interval):
    backend = plt.rcParams['backend']
    if backend in matplotlib.rcsetup.interactive_bk:
      figManager = matplotlib._pylab_helpers.Gcf.get_active()
      if figManager is not None:
        canvas = figManager.canvas
        if canvas.figure.stale:
          canvas.draw()
        canvas.start_event_loop(interval)
        return


class Cluster:
  def __init__(self, ):
    # coordinate map
    # xm = torch.linspace(0, 1, 480).view(
    #   1, 1, 1, -1).expand(1, 32, 480, 480)
    # ym = torch.linspace(0, 0.4, 480).view(
    #   1, 1, -1, 1).expand(1, 32, 480, 480)
    # zm = torch.linspace(0, 0.45, 32).view(
    #   1, -1, 1, 1).expand(1, 32, 480, 480)

    # coordinate map
    xm = torch.linspace(0, 2, 960).view(
      1, 1, 1, -1).expand(1, 32, 480, 960)
    ym = torch.linspace(0, 1, 480).view(
      1, 1, -1, 1).expand(1, 32, 480, 960)
    zm = torch.linspace(0, 0.1, 32).view(
      1, -1, 1, 1).expand(1, 32, 480, 960)

    xyzm = torch.cat((xm, ym, zm), 0)

    self.xyzm = xyzm.cuda()

  def cluster_with_gt(self, prediction, instance, n_sigma=1, ):

    height, width = prediction.size(1), prediction.size(2)

    xym_s = self.xyzm[:, 0:height, 0:width]  # 2 x h x w

    spatial_emb = torch.tanh(prediction[0:2]) + xym_s  # 2 x h x w
    sigma = prediction[2:2 + n_sigma]  # n_sigma x h x w

    instance_map = torch.zeros(height, width).byte().cuda()

    unique_instances = instance.unique()
    unique_instances = unique_instances[unique_instances != 0]

    for id in unique_instances:
      mask = instance.eq(id).view(1, height, width)

      center = spatial_emb[mask.expand_as(spatial_emb)].view(
        2, -1).mean(1).view(2, 1, 1)  # 2 x 1 x 1

      s = sigma[mask.expand_as(sigma)].view(n_sigma, -1).mean(1).view(n_sigma, 1, 1)
      s = torch.exp(s * 10)  # n_sigma x 1 x 1

      dist = torch.exp(-1 * torch.sum(torch.pow(spatial_emb - center, 2) * s, 0))

      proposal = (dist > 0.5)
      instance_map[proposal] = id

    return instance_map

  def cluster(self, prediction, n_sigma=1, threshold=0.5, iou_meter = None, in_mask = None):

    time, height, width = prediction.size(-3), prediction.size(-2), prediction.size(-1)
    xyzm_s = self.xyzm[:, 0:time, 0:height, 0:width]

    spatial_emb = torch.tanh(prediction[0:3]) + xyzm_s  # 3 x t x h x w
    sigma = prediction[3:3 + n_sigma]  # n_sigma x t x h x w
    # seed_map = torch.sigmoid(prediction[3 + n_sigma:3 + n_sigma + 1])  # 1 x t x h x w
    seed_map = prediction[3 + n_sigma:3 + n_sigma + 1]  # 1 x t x h x w

    instance_map = torch.zeros(time, height, width).byte()
    instances = []

    count = 1
    # mask = seed_map > 0.5
    mask = seed_map.bool()
    if mask.sum() > 128 * time:

      spatial_emb_masked = spatial_emb[mask.expand_as(spatial_emb)].view(3, -1)
      sigma_masked = sigma[mask.expand_as(sigma)].view(n_sigma, -1)
      seed_map_masked = seed_map[mask].view(1, -1)

      unclustered = torch.ones(mask.sum()).byte().cuda()
      instance_map_masked = torch.zeros(mask.sum()).byte().cuda()

      # track used masks for computing iou
      used_ids = {}
      while (unclustered.sum() > 128):
        seed = (seed_map_masked * unclustered.float()).argmax().item()
        seed_score = (seed_map_masked * unclustered.float()).max().item()
        if seed_score < threshold:
          break
        center = spatial_emb_masked[:, seed:seed + 1]
        unclustered[seed] = 0
        s = torch.exp(sigma_masked[:, seed:seed + 1] * 10)
        dist = torch.exp(-1 * torch.sum(torch.pow(spatial_emb_masked -
                                                  center, 2) * s, 0, keepdim=True))

        proposal = (dist > 0.5).squeeze()

        if proposal.sum() > 128:
          if unclustered[proposal].sum().float() / proposal.sum().float() > 0.5:
            instance_map_masked[proposal.squeeze()] = count
            instance_mask = torch.zeros(time, height, width).int()
            instance_mask[mask.squeeze().cpu()] = proposal.int().cpu()
            instances.append(
              {'mask': instance_mask.squeeze(), 'score': seed_score, 'centre': center})
            count += 1
            # calculate instance iou
            if iou_meter is not None and in_mask.shape[1] > 0:
              iou, id = get_best_overlap(instance_mask.numpy(),
                               in_mask.squeeze().data.cpu().numpy())
              if id not in used_ids.keys():
                used_ids[id] = calculate_iou(instance_mask.squeeze(), in_mask[id])
              elif iou > used_ids[id]:
                used_ids[id] = calculate_iou(instance_mask.squeeze(), in_mask[id])
              elif -1:
                iou_meter.update(0)
            elif in_mask.shape[1] == 0:
              iou_meter.update(0)

        unclustered[proposal] = 0

      instance_map[mask.squeeze().cpu()] = instance_map_masked.cpu()
      if in_mask.shape[1] == 0 and len(instances == 0):
        iou_meter.update(1)
      else:
        iou_meter.update(np.mean(list(used_ids.values())))

    return instance_map, instances


class Logger:
  def __init__(self, keys, title=""):

    self.data = {k: [] for k in keys}
    self.title = title
    self.win = None

    print('created logger with keys:  {}'.format(keys))

  def plot(self, save=False, save_dir=""):

    if self.win is None:
      self.win = plt.subplots()
    fig, ax = self.win
    ax.cla()

    keys = []
    for key in self.data:
      keys.append(key)
      data = self.data[key]
      ax.plot(range(len(data)), data, marker='.')

    ax.legend(keys, loc='upper right')
    ax.set_title(self.title)

    plt.draw()
    Visualizer.mypause(0.001)

    if save:
      # save figure
      fig.savefig(os.path.join(save_dir, self.title + '.png'))

      # save data as csv
      df = pd.DataFrame.from_dict(self.data)
      df.to_csv(os.path.join(save_dir, self.title + '.csv'))

  def add(self, key, value):
    assert key in self.data, "Key not in data"
    self.data[key].append(value)