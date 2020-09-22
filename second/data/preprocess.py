from pathlib import Path
import pathlib
import pickle
import time
from collections import defaultdict
from numpy import meshgrid as n_mesh
from second.core.box_np_ops import camera_to_lidar
from second.core import box_np_ops
    
import cv2
import numpy as np
from skimage import io as imgio
from PIL import Image
import os
from second.core import box_np_ops
from second.core import preprocess as prep
from second.core.geometry import points_in_convex_polygon_3d_jit
from second.data import kitti_common as kitti
from second.utils import make_idx
import torch


def merge_second_batch(batch_list, _unused=False):
    example_merged = defaultdict(list)
    for example in batch_list:
        for k, v in example.items():
            example_merged[k].append(v)
    ret = {}
    example_merged.pop("num_voxels")
    for key, elems in example_merged.items():
        if key in [
                'voxels', 'num_points', 'num_gt', 'gt_boxes', 'voxel_labels',
                'match_indices'
        ]:
            ret[key] = np.concatenate(elems, axis=0)
        elif key == 'match_indices_num':
            ret[key] = np.concatenate(elems, axis=0)
        elif key == 'coordinates':
            coors = []
            for i, coor in enumerate(elems):
                coor_pad = np.pad(
                    coor, ((0, 0), (1, 0)),
                    mode='constant',
                    constant_values=i)
                coors.append(coor_pad)
            ret[key] = np.concatenate(coors, axis=0)
        else:
            ret[key] = np.stack(elems, axis=0)
    return ret


def merge_second_batch_multigpu(batch_list):
    example_merged = defaultdict(list)
    for example in batch_list:
        for k, v in example.items():
            example_merged[k].append(v)
    ret = {}
    for key, elems in example_merged.items():
        if key == 'metadata':
            ret[key] = elems
        elif key == "calib":
            ret[key] = {}
            for elem in elems:
                for k1, v1 in elem.items():
                    if k1 not in ret[key]:
                        ret[key][k1] = [v1]
                    else:
                        ret[key][k1].append(v1)
            for k1, v1 in ret[key].items():
                ret[key][k1] = np.stack(v1, axis=0)
        elif key == 'coordinates':
            coors = []
            for i, coor in enumerate(elems):
                coor_pad = np.pad(
                    coor, ((0, 0), (1, 0)), mode='constant', constant_values=i)
                coors.append(coor_pad)
            ret[key] = np.stack(coors, axis=0)
        elif key in ['gt_names', 'gt_classes', 'gt_boxes']:
            continue
        else:
            ret[key] = np.stack(elems, axis=0)
        
    return ret


def prep_pointcloud(input_dict,
                    root_path,
                    voxel_generator,
                    target_assigner,
                    db_sampler=None,
                    max_voxels=20000,
                    class_names=['Car'],
                    remove_outside_points=False,
                    training=True,
                    create_targets=True,
                    shuffle_points=False,
                    reduce_valid_area=False,
                    remove_unknown=False,
                    gt_rotation_noise=[-np.pi / 3, np.pi / 3],
                    gt_loc_noise_std=[1.0, 1.0, 1.0],
                    global_rotation_noise=[-np.pi / 4, np.pi / 4],
                    global_scaling_noise=[0.95, 1.05],
                    global_random_rot_range=[0.78, 2.35],
                    generate_bev=False,
                    without_reflectivity=False,
                    num_point_features=4,
                    anchor_area_threshold=1,
                    gt_points_drop=0.0,
                    gt_drop_max_keep=10,
                    remove_points_after_sample=True,
                    anchor_cache=None,
                    remove_environment=False,
                    random_crop=False,
                    reference_detections=None,
                    add_rgb_to_points=False,
                    lidar_input=False,
                    unlabeled_db_sampler=None,
                    out_size_factor=2,
                    min_gt_point_dict=None,
                    bev_only=False,
                    use_group_id=False,
                    out_dtype=np.float32):
    """convert point cloud to voxels, create targets if ground truths 
    exists.
    """
    f_view = _read_and_process_f_view(np.asarray(input_dict['cam']))
    # f_view_r = _read_and_process_f_view(np.asarray(input_dict['cam_r']))
    points = input_dict["points"]

    if training:
        gt_boxes = input_dict["gt_boxes"]
        gt_names = input_dict["gt_names"]
        difficulty = input_dict["difficulty"]
        group_ids = None
        if use_group_id and "group_ids" in input_dict:
            group_ids = input_dict["group_ids"]
    rect = input_dict["rect"]
    Trv2c = input_dict["Trv2c"]
    P2 = input_dict["P2"]
    # P3 = input_dict["P3"]
    unlabeled_training = unlabeled_db_sampler is not None
    image_idx = input_dict["image_idx"]

    if reference_detections is not None:
        C, R, T = box_np_ops.projection_matrix_to_CRT_kitti(P2)
        frustums = box_np_ops.get_frustum_v2(reference_detections, C)
        frustums -= T
        # frustums = np.linalg.inv(R) @ frustums.T
        frustums = np.einsum('ij, akj->aki', np.linalg.inv(R), frustums)
        frustums = box_np_ops.camera_to_lidar(frustums, rect, Trv2c)
        surfaces = box_np_ops.corner_to_surfaces_3d_jit(frustums)
        masks = points_in_convex_polygon_3d_jit(points, surfaces)
        points = points[masks.any(-1)]

    if remove_outside_points and not lidar_input:
        image_shape = input_dict["image_shape"]
        points = box_np_ops.remove_outside_points(points, rect, Trv2c, P2,
                                                  image_shape)
    if remove_environment is True and training:
        selected = kitti.keep_arrays_by_name(gt_names, class_names)
        gt_boxes = gt_boxes[selected]
        gt_names = gt_names[selected]
        difficulty = difficulty[selected]
        if group_ids is not None:
            group_ids = group_ids[selected]
        points = prep.remove_points_outside_boxes(points, gt_boxes)
    if training:
        # print(gt_names)
        selected = kitti.drop_arrays_by_name(gt_names, ["DontCare"])
        gt_boxes = gt_boxes[selected]
        gt_names = gt_names[selected]
        difficulty = difficulty[selected]
        if group_ids is not None:
            group_ids = group_ids[selected]

        gt_boxes = box_np_ops.box_camera_to_lidar(gt_boxes, rect, Trv2c)
        if remove_unknown:
            remove_mask = difficulty == -1
            """
            gt_boxes_remove = gt_boxes[remove_mask]
            gt_boxes_remove[:, 3:6] += 0.25
            points = prep.remove_points_in_boxes(points, gt_boxes_remove)
            """
            keep_mask = np.logical_not(remove_mask)
            gt_boxes = gt_boxes[keep_mask]
            gt_names = gt_names[keep_mask]
            difficulty = difficulty[keep_mask]
            if group_ids is not None:
                group_ids = group_ids[keep_mask]
        gt_boxes_mask = np.array(
            [n in class_names for n in gt_names], dtype=np.bool_)
        
        ## gt sampling on/off
        if db_sampler is not None:
        # if False:
            sampled_dict = db_sampler.sample_all(
                root_path,
                gt_boxes,
                gt_names,
                num_point_features,
                random_crop,
                gt_group_ids=group_ids,
                rect=rect,
                Trv2c=Trv2c,
                P2=P2)

            if sampled_dict is not None:
                sampled_gt_names = sampled_dict["gt_names"]
                sampled_gt_boxes = sampled_dict["gt_boxes"]
                sampled_points = sampled_dict["points"]
                sampled_gt_masks = sampled_dict["gt_masks"]
                # gt_names = gt_names[gt_boxes_mask].tolist()
                gt_names = np.concatenate([gt_names, sampled_gt_names], axis=0)
                # gt_names += [s["name"] for s in sampled]
                gt_boxes = np.concatenate([gt_boxes, sampled_gt_boxes])
                gt_boxes_mask = np.concatenate(
                    [gt_boxes_mask, sampled_gt_masks], axis=0)
                if group_ids is not None:
                    sampled_group_ids = sampled_dict["group_ids"]
                    group_ids = np.concatenate([group_ids, sampled_group_ids])

                if remove_points_after_sample:
                    points = prep.remove_points_in_boxes(
                        points, sampled_gt_boxes)

                points = np.concatenate([sampled_points, points], axis=0)
        # unlabeled_mask = np.zeros((gt_boxes.shape[0], ), dtype=np.bool_)
        if without_reflectivity:
            used_point_axes = list(range(num_point_features))
            used_point_axes.pop(3)
            points = points[:, used_point_axes]
        pc_range = voxel_generator.point_cloud_range
        if bev_only:  # set z and h to limits
            gt_boxes[:, 2] = pc_range[2]
            gt_boxes[:, 5] = pc_range[5] - pc_range[2]
        prep.noise_per_object_v3_(
            gt_boxes,
            points,
            gt_boxes_mask,
            rotation_perturb=gt_rotation_noise,
            center_noise_std=gt_loc_noise_std,
            global_random_rot_range=global_random_rot_range,
            group_ids=group_ids,
            num_try=100)
        # should remove unrelated objects after noise per object
        gt_boxes = gt_boxes[gt_boxes_mask]
        gt_names = gt_names[gt_boxes_mask]
        if group_ids is not None:
            group_ids = group_ids[gt_boxes_mask]
        gt_classes = np.array(
            [class_names.index(n) + 1 for n in gt_names], dtype=np.int32)
        gt_boxes, points, flip_y = prep.random_flip(gt_boxes, points) #, probability=0.)
        gt_boxes, points, rot_noise = prep.global_rotation(
            gt_boxes, points, rotation=global_rotation_noise)
        gt_boxes, points, scal_noise = prep.global_scaling_v2(gt_boxes, points,
                                                  *global_scaling_noise)

        bv_range = voxel_generator.point_cloud_range[[0, 1, 3, 4]]
        mask = prep.filter_gt_box_outside_range(gt_boxes, bv_range)
        gt_boxes = gt_boxes[mask]
        gt_classes = gt_classes[mask]
        gt_names = gt_names[mask]
        if group_ids is not None:
            group_ids = group_ids[mask]

        # limit rad to [-pi, pi]
        gt_boxes[:, 6] = box_np_ops.limit_period(
            gt_boxes[:, 6], offset=0.5, period=2 * np.pi)
        if flip_y:
            f_view = np.flip(f_view, 1) ## flip x_dim
            # f_view_r = np.flip(f_view_r, 1) ## flip x_dim
    else:
        rot_noise = 0.0
        scal_noise = 1.0

    if shuffle_points:
        # shuffle is a little slow.
        np.random.shuffle(points)

    calib = {
        'rect': input_dict['rect'],
        'Trv2c': input_dict['Trv2c'],
        'P2': input_dict['P2'],
        # 'P3': input_dict['P3'],
        }
    idxs_norm = _get_depth_idx(calib, f_view, image_idx, rot_noise, scal_noise)
    idxs_norm_ori = _get_fview_idx(calib, f_view, rot_noise, scal_noise)
    # idxs_norm_ori_r = _get_fview_idx(calib, f_view_r, rot_noise, scal_noise, right=True)
    # import pdb; pdb.set_trace()
# 
    # [0, -40, -3, 70.4, 40, 1]
    voxel_size = voxel_generator.voxel_size
    pc_range = voxel_generator.point_cloud_range
    grid_size = voxel_generator.grid_size
    # [352, 400]

    voxels, coordinates, num_points = voxel_generator.generate(
        points, max_voxels)

    example = {
        'voxels': voxels,
        'num_points': num_points,
        'coordinates': coordinates,
        "num_voxels": np.array([voxels.shape[0]], dtype=np.int64),
        'f_view': f_view.transpose(0,2,1),
        # 'f_view_r': f_view_r.transpose(0,2,1),
        'idxs_norm': idxs_norm,
        'idxs_norm_ori': idxs_norm_ori,
        # 'idxs_norm_ori_r': idxs_norm_ori_r,
    }
    example.update({
        'rect': rect,
        'Trv2c': Trv2c,
        'P2': P2,
        # 'P3': P3,
    })
    # if not lidar_input:
    feature_map_size = grid_size[:2] // out_size_factor
    feature_map_size = [*feature_map_size, 1][::-1]
    if anchor_cache is not None:
        anchors = anchor_cache["anchors"]
        anchors_bv = anchor_cache["anchors_bv"]
        matched_thresholds = anchor_cache["matched_thresholds"]
        unmatched_thresholds = anchor_cache["unmatched_thresholds"]
        anchors_dict = anchor_cache["anchors_dict"]
    else:
        ret = target_assigner.generate_anchors(feature_map_size)
        anchors = ret["anchors"]
        anchors = anchors.reshape([-1, 7])
        matched_thresholds = ret["matched_thresholds"]
        unmatched_thresholds = ret["unmatched_thresholds"]
        anchors_dict = target_assigner.generate_anchors_dict(feature_map_size)
        anchors_bv = box_np_ops.rbbox2d_to_near_bbox(
            anchors[:, [0, 1, 3, 4, 6]])
    example["anchors"] = anchors
    # print("debug", anchors.shape, matched_thresholds.shape)
    # anchors_bv = anchors_bv.reshape([-1, 4])
    anchors_mask = None
    if anchor_area_threshold >= 0:
        coors = coordinates
        dense_voxel_map = box_np_ops.sparse_sum_for_anchors_mask(
            coors, tuple(grid_size[::-1][1:]))
        dense_voxel_map = dense_voxel_map.cumsum(0)
        dense_voxel_map = dense_voxel_map.cumsum(1)
        anchors_area = box_np_ops.fused_get_anchors_area(
            dense_voxel_map, anchors_bv, voxel_size, pc_range, grid_size)
        anchors_mask = anchors_area > anchor_area_threshold
        # example['anchors_mask'] = anchors_mask.astype(np.uint8)
        example['anchors_mask'] = anchors_mask
    if not training:
        return example
    if create_targets:
        targets_dict = target_assigner.assign_v2(
            anchors_dict,
            gt_boxes,
            anchors_mask,
            gt_classes=gt_classes,
            gt_names=gt_names)
        example.update({
            'labels': targets_dict['labels'],
            'reg_targets': targets_dict['bbox_targets'],
            'reg_weights': targets_dict['bbox_outside_weights'],
        })
    return example


def _read_and_prep_v9(info, root_path, num_point_features, prep_func):
    """read data from KITTI-format infos, then call prep function.
    """
    read_image = True
    # read_stereo = True
    # velodyne_path = str(pathlib.Path(root_path) / info['velodyne_path'])
    # velodyne_path += '_reduced'
    v_path = pathlib.Path(root_path) / info['velodyne_path']
    v_path = v_path.parent.parent / (
        v_path.parent.stem + "_reduced") / v_path.name

    points = np.fromfile(
        str(v_path), dtype=np.float32,
        count=-1).reshape([-1, num_point_features])
    image_idx = info['image_idx']
    rect = info['calib/R0_rect'].astype(np.float32)
    Trv2c = info['calib/Tr_velo_to_cam'].astype(np.float32)
    P2 = info['calib/P2'].astype(np.float32)
    # P3 = info['calib/P3'].astype(np.float32)
    image_path = info['img_path']
    if read_image:
        image_path = Path(root_path) / image_path
        image_str = Image.open(os.path.join(image_path))
    # import pdb; pdb.set_trace()
    # if read_stereo:
        # image_path_r = Path(root_path) / info['img_path'].replace('image_2', 'image_3')
        # image_str_r = Image.open(os.path.join(image_path_r))

    input_dict = {
        'points': points,
        'rect': rect,
        'Trv2c': Trv2c,
        'P2': P2,
        # 'P3': P3,
        'image_shape': np.array(info["img_shape"], dtype=np.int32),
        'image_idx': image_idx,
        'cam': image_str,
        'image_path': info['img_path'],
        # 'cam_r': image_str_r,
        # 'image_path_r': info['img_path'].replace('image_2', 'image_3'),
        # 'pointcloud_num_features': num_point_features,
    }

    if 'annos' in info:
        annos = info['annos']
        # we need other objects to avoid collision when sample
        annos = kitti.remove_dontcare(annos)
        loc = annos["location"]
        dims = annos["dimensions"]
        rots = annos["rotation_y"]
        gt_names = annos["name"]
        # print(gt_names, len(loc))
        gt_boxes = np.concatenate(
            [loc, dims, rots[..., np.newaxis]], axis=1).astype(np.float32)
        # gt_boxes = box_np_ops.box_camera_to_lidar(gt_boxes, rect, Trv2c)
        difficulty = annos["difficulty"]
        input_dict.update({
            'gt_boxes': gt_boxes,
            'gt_names': gt_names,
            'difficulty': difficulty,
        })
        if 'group_ids' in annos:
            input_dict['group_ids'] = annos["group_ids"]
    example = prep_func(input_dict=input_dict)
    example["image_idx"] = image_idx
    example["image_shape"] = input_dict["image_shape"]
    if "anchors_mask" in example:
        example["anchors_mask"] = example["anchors_mask"].astype(np.uint8)
    return example

def _read_and_process_f_view(f_view):
    # f_view = cv2.resize(f_view, (2496,768), interpolation = cv2.INTER_CUBIC)
    # import pdb; pdb.set_trace()
    
    f_view = cv2.resize(f_view, (1248,384))
    # f_view = np.roll(f_view, -16, axis=0)
    # f_view = np.roll(f_view, -16, axis=1)
    
    f_view = f_view.astype('float32')
    f_mean = np.array([0.485,0.456,0.406])
    f_mean = np.expand_dims(f_mean, 1)
    f_mean = np.expand_dims(f_mean, 1)
    f_std = np.array([0.229,0.224,0.225])
    f_std = np.expand_dims(f_std, 1)
    f_std = np.expand_dims(f_std, 1)
    f_view = f_view.transpose(2,1,0) ## for get idx
    f_view = (f_view/255. - f_mean) / f_std
    return f_view

def _get_depth_idx(calib, f_view, img_idx, rot_noise, scal_noise):
    d_path = '/mnt/sdb/jhyoo/new_15_second/second.pytorch/second/dataset/depth_maps/trainval'
    depth = np.load(os.path.join(d_path, '{0:06d}'.format(img_idx)+'.npy'))
    r_depth = np.asarray(cv2.resize(depth, (156,48)))
    # r_depth = np.asarray(cv2.resize(depth, (1248,384)))

    c, r = n_mesh(np.arange(156), np.arange(48))
    # c, r = n_mesh(np.arange(1248), np.arange(384))
    r_depth = np.ones([48,156])*30.
    points = np.stack([c*8+4, r*8+4, r_depth])
    # import pdb; pdb.set_trace()
    # points = np.stack([c, r, r_depth])
    points = points.reshape((3, -1))
    points = points.T
    V2C = calib['Trv2c'][:3,:]
    # import pdb; pdb.set_trace()
    C2V = inverse_rigid_trans(V2C)
    P = calib['P2']
    c_u = P[0, 2]
    c_v = P[1, 2]
    f_u = P[0, 0]
    f_v = P[1, 1]
    b_x = P[0, 3] / (-f_u)  # relative
    b_y = P[1, 3] / (-f_v)
    uv_depth = points
    n = uv_depth.shape[0]
    x = ((uv_depth[:, 0] - c_u) * uv_depth[:, 2]) / f_u + b_x
    y = ((uv_depth[:, 1] - c_v) * uv_depth[:, 2]) / f_v + b_y
    pts_3d_rect = np.zeros((n, 3))
    pts_3d_rect[:, 0] = x
    pts_3d_rect[:, 1] = y
    pts_3d_rect[:, 2] = uv_depth[:, 2]
    R0 = calib['rect'][:3, :3]
    pts_3d_rect = np.transpose(np.dot(np.linalg.inv(R0), np.transpose(pts_3d_rect)))
    n = pts_3d_rect.shape[0]
    pts_3d_rect = np.hstack((pts_3d_rect, np.ones((n, 1))))
    pts_3d_rect = np.dot(pts_3d_rect, np.transpose(C2V))
    # return pts_3d_hom
    cloud = pts_3d_rect
    
    cloud = box_np_ops.rotation_points_single_angle(cloud, rot_noise, axis=2)
    cloud *= scal_noise

    # cloud = camera_to_lidar(points, calib['rect'], calib['Trv2c'])
    validx = (cloud[:, 0] >= 0) & (cloud[:, 0] <= 70.4)
    validy = (cloud[:, 1] >= -40.0) & (cloud[:, 1] <= 40.0)
    # validz = (cloud[:, 2] >= -3.0) & (cloud[:, 2] <= 1.0)
    validz = (cloud[:, 2] >= -2.0) & (cloud[:, 2] <= -1.4)
    valid = validx & validy & validz
    clouds = cloud * valid.reshape(-1,1)
    import pdb; pdb.set_trace()
    # import pdb; pdb.set_trace()
    clouds = torch.tensor(clouds)
    clouds[:,0] = clouds[:,0]/70.4*176
    clouds[:,1] = (clouds[:,1]+40)/80*200
    return clouds

def _get_fview_idx(calib, f_view, rot_noise, scal_noise, right=False):
    import torch
    # idx_num = [-17., -25., -10.]
    idx_num = [-17., -19., -15.]
    idx_all = []
    for i in range(len(idx_num)):
        idxs, idxs_norm_0 = make_idx.get_projected_idx([704.,800.], calib, f_view.shape, idx_num[i], rot_noise, scal_noise, 4., right=right)
        idx_all.append(idxs_norm_0)
    idxs_norm = torch.stack((idx_all),dim=0)
    return idxs_norm

def inverse_rigid_trans(Tr):
    ''' Inverse a rigid body transform matrix (3x4 as [R|t])
        [R'|-R't; 0|1]
    '''
    inv_Tr = np.zeros_like(Tr)  # 3x4
    inv_Tr[0:3, 0:3] = np.transpose(Tr[0:3, 0:3])
    inv_Tr[0:3, 3] = np.dot(-np.transpose(Tr[0:3, 0:3]), Tr[0:3, 3])
    return inv_Tr