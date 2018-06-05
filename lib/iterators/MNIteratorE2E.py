import matplotlib
import matplotlib.pyplot as plt
import mxnet as mx
import numpy as np
from MNIteratorBase import MNIteratorBase
from multiprocessing import Pool
from utils.data_workers import roidb_anchor_worker, im_worker, props_in_chip_worker, chip_worker

class MNIteratorE2E(MNIteratorBase):
    def __init__(self, roidb, config, batch_size=4, threads=8, nGPUs=1, pad_rois_to=400, crop_size=(512, 512)):
        self.crop_size = crop_size
        self.num_classes = roidb[0]['gt_overlaps'].shape[1]
        self.bbox_means = np.tile(np.array(config.TRAIN.BBOX_MEANS), (self.num_classes, 1))
        self.bbox_stds = np.tile(np.array(config.TRAIN.BBOX_STDS), (self.num_classes, 1))
        self.data_name = ['data', 'valid_ranges', 'im_info']
        self.label_name = ['label', 'bbox_target', 'bbox_weight', 'gt_boxes']
        if config.TRAIN.WITH_MASK:
            self.label_name.append('gt_masks')
        self.pool = Pool(64)
        self.context_size = 320
        self.epiter = 0
        self.im_worker = im_worker(crop_size=self.crop_size[0], cfg=config)
        super(MNIteratorE2E, self).__init__(roidb, config, batch_size, threads, nGPUs, pad_rois_to, False)

    def reset(self):
        self.cur_i = 0
        self.n_neg_per_im = 2
        self.crop_idx = [0] * len(self.roidb)
        chips = self.pool.map(chip_worker, self.roidb)
        chip_count = 0
        for i, r in enumerate(self.roidb):
            cs = chips[i]
            chip_count += len(cs)
            r['crops'] = cs

        all_props_in_chips = self.pool.map(props_in_chip_worker, self.roidb)

        for (props_in_chips, neg_chips, neg_props_in_chips), cur_roidb in zip(all_props_in_chips, self.roidb):
            cur_roidb['props_in_chips'] = props_in_chips
            cur_roidb['neg_crops'] = neg_chips
            cur_roidb['neg_props_in_chips'] = neg_props_in_chips

        # Append negative chips
        chipindex = []
        for i, r in enumerate(self.roidb):
            cs = r['neg_crops']
            if len(cs) > 0:
                sel_inds = np.arange(len(cs))
                if len(cs) > self.n_neg_per_im:
                    sel_inds = np.random.permutation(sel_inds)[0:self.n_neg_per_im]
                for ind in sel_inds:
                    chip_count = chip_count + 1
                    r['crops'].append(r['neg_crops'][ind])
                    r['props_in_chips'].append(r['neg_props_in_chips'][ind].astype(np.int32))
            all_crops = r['crops']
            for j in range(len(all_crops)):
                chipindex.append(i)
        print('quack N chips: {}'.format(chip_count))
        blocksize = self.batch_size
        chipindex = np.array(chipindex)
        if chipindex.shape[0] % blocksize > 0:
            extra = blocksize - (chipindex.shape[0] % blocksize)
            chipindex = np.hstack((chipindex, chipindex[0:extra]))
        allinds = np.random.permutation(chipindex)
        self.inds = np.array(allinds, dtype=int)
        for r in self.roidb:
            r['chip_order'] = np.random.permutation(np.arange(len(r['crops'])))

        self.epiter = self.epiter + 1
        self.size = len(self.inds)
        print 'Done!'

    def get_batch(self):
        if self.cur_i >= self.size:
            return False
        self.batch = self._get_batch()
        self.cur_i += self.batch_size
        return True

    def _get_batch(self):
        """
        Get batch for training the RPN (optionaly with mask information)
        """
        cur_from = self.cur_i
        cur_to = self.cur_i + self.batch_size
        roidb = [self.roidb[self.inds[i]] for i in range(cur_from, cur_to)]

        cropids = [self.roidb[self.inds[i]]['chip_order'][
                       self.crop_idx[self.inds[i]] % len(self.roidb[self.inds[i]]['chip_order'])] for i in
                   range(cur_from, cur_to)]
        n_batch = len(roidb)
        crops = [roidb[i]['crops'][cropids[i]] for i in range(n_batch)]

        ims = []
        for i in range(n_batch):
            ims.append([roidb[i]['image'], roidb[i]['crops'][cropids[i]], roidb[i]['flipped']])

        for i in range(cur_from, cur_to):
            self.crop_idx[self.inds[i]] = self.crop_idx[self.inds[i]] + 1

        processed_roidb = []
        for i in range(len(roidb)):
            tmp = roidb[i].copy()
            scale = roidb[i]['crops'][cropids[i]][1]
            height = roidb[i]['crops'][cropids[i]][2]
            width = roidb[i]['crops'][cropids[i]][3]                                      
            tmp['im_info'] = [self.crop_size[0], self.crop_size[1], scale]
            processed_roidb.append(tmp)

        processed_list = self.thread_pool.map_async(self.im_worker.worker, ims)

        worker_data = []
        srange = np.zeros((len(processed_roidb), 2))
        chipinfo = np.zeros((len(processed_roidb), 3))
        for i in range(len(processed_roidb)):
            cropid = cropids[i]
            nids = processed_roidb[i]['props_in_chips'][cropid]
            gtids = np.where(processed_roidb[i]['max_overlaps'] == 1)[0]
            gt_boxes = processed_roidb[i]['boxes'][gtids, :]
            boxes = processed_roidb[i]['boxes'].copy()
            cur_crop = processed_roidb[i]['crops'][cropid][0]
            im_scale = processed_roidb[i]['crops'][cropid][1]
            height = processed_roidb[i]['crops'][cropid][2]
            width = processed_roidb[i]['crops'][cropid][3]
            classes = processed_roidb[i]['max_classes'][gtids]
            gt_masks = processed_roidb[i]['gt_masks']
            if im_scale == 3:
                srange[i, 0] = 0
                srange[i, 1] = 80*3
            elif im_scale == 1.667:
                srange[i, 0] = 32*1.667
                srange[i, 1] = 1.667*150
            else:
                srange[i, 0] = 120*im_scale
                srange[i, 1] = 512
            chipinfo[i, 0] = height
            chipinfo[i, 1] = width
            chipinfo[i, 2] = im_scale
            argw = [processed_roidb[i]['im_info'], cur_crop, im_scale, nids, gtids, gt_boxes, boxes,
             classes.reshape(len(classes), 1), gt_masks, processed_roidb[i]['image']]
            worker_data.append(argw)

        all_labels = self.pool.map(roidb_anchor_worker, worker_data)

        A = 21
        feat_height = 32
        feat_width = 32
        labels = mx.nd.zeros((n_batch, A * feat_height * feat_width), mx.cpu(0))
        bbox_targets = mx.nd.zeros((n_batch, A * 4, feat_height, feat_width), mx.cpu(0))
        bbox_weights = mx.nd.zeros((n_batch, A * 4, feat_height, feat_width), mx.cpu(0))
        gt_boxes = -mx.nd.ones((n_batch, 100, 5))
        encoded_masks = -mx.nd.ones((n_batch,100,500))

        for i in range(len(all_labels)):
            labels[i] = all_labels[i][0][0]
            pids = all_labels[i][2]
            if len(pids[0]) > 0:
                bbox_targets[i][pids[0], pids[1], pids[2]] = all_labels[i][1]
                bbox_weights[i][pids[0], pids[1], pids[2]] = 1.0
            gt_boxes[i] = all_labels[i][3]
            encoded_masks[i] = all_labels[i][4]


        im_tensor = mx.nd.zeros((n_batch, 3, self.crop_size[0], self.crop_size[1]), dtype=np.float32)
        processed_list = processed_list.get()
        for i in range(len(processed_list)):
            im_tensor[i] = processed_list[i]


        self.data = [im_tensor, mx.nd.array(srange), mx.nd.array(chipinfo)]
        self.label = [labels, bbox_targets, bbox_weights, gt_boxes, 
        mx.nd.array(encoded_masks)]

        return mx.io.DataBatch(data=self.data, label=self.label, pad=self.getpad(), index=self.getindex(),
                               provide_data=self.provide_data, provide_label=self.provide_label)


    def visualize(self, im_tensor, boxes):
        # import pdb;pdb.set_trace()
        im_tensor = im_tensor.asnumpy()
        boxes = boxes.asnumpy()

        for imi in range(im_tensor.shape[0]):
            im = np.zeros((im_tensor.shape[2], im_tensor.shape[3], 3), dtype=np.uint8)
            for i in range(3):
                im[:, :, i] = im_tensor[imi, i, :, :] + self.pixel_mean[2 - i]
            # Visualize positives
            plt.imshow(im)
            cboxes = boxes[imi]
            # cboxes = boxes[imi][:, 0:4]
            for box in cboxes:
                rect = plt.Rectangle((box[0], box[1]),
                                     box[2] - box[0],
                                     box[3] - box[1], fill=False,
                                     edgecolor='green', linewidth=3.5)
                plt.gca().add_patch(rect)
            num = np.random.randint(100000)
            # plt.show()
            plt.savefig('debug/visualization/test_{}_pos.png'.format(num))
            plt.cla()
            plt.clf()
            plt.close()