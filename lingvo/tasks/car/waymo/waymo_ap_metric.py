# Lint as: python2, python3
# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Average Precision metric class for Waymo open dataset.

The Waymo library provides a metrics breakdown API for a set of breakdowns
implemented in their library.  This wrapper uses our basic abstraction for
building AP metrics but only allows breakdowns that are supported in the
Waymo breakdown API.  Should you want other breakdowns, consider using the
standard AP metrics implementation with our custom breakdowns.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from lingvo import compat as tf
from lingvo.core import plot
from lingvo.core import py_utils
from lingvo.tasks.car import ap_metric
from lingvo.tasks.car import breakdown_metric

import numpy as np
from six.moves import range
from waymo_open_dataset import label_pb2
from waymo_open_dataset.metrics.ops import py_metrics_ops
from waymo_open_dataset.metrics.python import config_util_py as config_util
from waymo_open_dataset.protos import breakdown_pb2
from waymo_open_dataset.protos import metrics_pb2


def _BuildWaymoMetricConfig(metadata, box_type, waymo_breakdown_metrics):
  """Build the Config proto for Waymo's metric op."""
  config = metrics_pb2.Config()
  # config.num_desired_score_cutoffs = metadata.NumberOfPrecisionRecallPoints()
  num_pr_points = metadata.NumberOfPrecisionRecallPoints()
  config.score_cutoffs.extend(
      [i * 1.0 / (num_pr_points - 1) for i in range(num_pr_points)])
  config.matcher_type = metrics_pb2.MatcherProto.Type.TYPE_HUNGARIAN
  if box_type == '2d':
    config.box_type = label_pb2.Label.Box.Type.TYPE_2D
  else:
    config.box_type = label_pb2.Label.Box.Type.TYPE_3D
  # Default values
  config.iou_thresholds[:] = [0.7, 0.7, 0.7, 0.7, 0.7]
  for class_name, threshold in metadata.IoUThresholds().items():
    cls_idx = metadata.ClassNames().index(class_name)
    config.iou_thresholds[cls_idx] = threshold
  config.breakdown_generator_ids.append(breakdown_pb2.Breakdown.ONE_SHARD)
  config.difficulties.append(metrics_pb2.Difficulty())
  # Add extra breakdown metrics.
  for breakdown_value in waymo_breakdown_metrics:
    breakdown_id = breakdown_pb2.Breakdown.GeneratorId.Value(breakdown_value)
    config.breakdown_generator_ids.append(breakdown_id)
    config.difficulties.append(metrics_pb2.Difficulty())
  return config


class WaymoAPMetrics(ap_metric.APMetrics):
  """The Waymo Open Dataset implementation of AP metric."""

  @classmethod
  def Params(cls, metadata):
    """Params builder for APMetrics."""
    p = super(WaymoAPMetrics, cls).Params(metadata)
    p.Define(
        'waymo_breakdown_metrics', [],
        'List of extra waymo breakdown metrics when computing AP. These '
        'should match the names of the proto entries in metrics.proto, such '
        'as `RANGE` or `OBJECT_TYPE`.')
    return p

  def __init__(self, params):
    super(WaymoAPMetrics, self).__init__(params)
    self._waymo_metric_config = _BuildWaymoMetricConfig(
        self.metadata, self.params.box_type,
        self.params.waymo_breakdown_metrics)
    # Compute only waymo breakdown metrics.
    breakdown_names = config_util.get_breakdown_names_from_config(
        self._waymo_metric_config)
    waymo_params = WaymoBreakdownMetric.Params().Set(
        metadata=self.metadata, breakdown_list=breakdown_names)
    self._breakdown_metrics['waymo'] = WaymoBreakdownMetric(waymo_params)

    # Remove the base metric.
    del self._breakdown_metrics['difficulty']

  def _GetData(self,
               classid,
               difficulty=None,
               distance=None,
               num_points=None,
               rotation=None):
    """Returns groundtruth and prediction for the classid in a NestedMap.

    Args:
      classid: int32 specifying the class
      difficulty: String in [easy, moderate, hard]. If None specified, all
        difficulty levels are permitted.
      distance: int32 specifying a binned Euclidean distance of the ground truth
        bounding box. If None is specified, all distances are selected.
      num_points: int32 specifying a binned number of laser points within the
        ground truth bounding box. If None is specified, all boxes are selected.
      rotation: int32 specifying a binned rotation within the ground truth
        bounding box. If None is specified, all boxes are selected.

    Returns:
      NestedMap containing iou_threshold, groundtruth and predictions for
      specified, classid, difficulty level and binned distance. If no bboxes
      are found with these parameters, returns None.
    """
    assert classid > 0 and classid < self.metadata.NumClasses()
    g = self._LoadBoundingBoxes(
        'groundtruth',
        classid,
        distance=distance,
        num_points=num_points,
        rotation=rotation)
    # Note that we do not specify num_points for predictions because only
    # groundtruth boxes contain points.
    p = self._LoadBoundingBoxes(
        'prediction', classid, distance, num_points=None, rotation=rotation)
    if g is None or p is None:
      return None

    gt_boxes = g.boxes
    gt_imgids = g.imgids
    gt_speeds = g.speeds
    iou_threshold = self._iou_thresholds[self.metadata.ClassNames()[classid]]

    return py_utils.NestedMap(
        iou_threshold=iou_threshold,
        gt=py_utils.NestedMap(imgid=gt_imgids, bbox=gt_boxes, speed=gt_speeds),
        pd=py_utils.NestedMap(imgid=p.imgids, bbox=p.boxes, score=p.scores))

  def _BuildMetric(self, feed_data, classid):
    """Construct tensors and the feed_dict for Waymo metric op.

    Args:
      feed_data: a NestedMap returned by _GetData().
      classid: integer.

    Returns:
      A tuple of 3 dicts:

      - scalar_metrics: a dict mapping all the metric names to fetch tensors.
      - curves: a dict mapping all the curve names to fetch tensors.
      - feed_dict: a dict mapping the tensors in feed_tensors to feed values.
    """
    breakdown_names = config_util.get_breakdown_names_from_config(
        self._waymo_metric_config)
    if feed_data is None:
      dummy_scalar = tf.constant(np.nan)
      dummy_curve = tf.zeros([self.metadata.NumberOfPrecisionRecallPoints(), 2],
                             tf.float32)
      scalar_metrics = {'ap': dummy_scalar, 'ap_ha_weighted': dummy_scalar}
      curve_metrics = {'pr': dummy_curve, 'pr_ha_weighted': dummy_curve}

      for i, metric in enumerate(breakdown_names):
        scalar_metrics['ap_%s' % metric] = dummy_scalar
        scalar_metrics['ap_ha_weighted_%s' % metric] = dummy_scalar
        curve_metrics['pr_%s' % metric] = dummy_curve
        curve_metrics['pr_ha_weighted_%s' % metric] = dummy_curve
      return scalar_metrics, curve_metrics, {}

    feed_dict = {}

    f_gt_bbox = tf.placeholder(tf.float32)
    feed_dict[f_gt_bbox] = feed_data.gt.bbox

    f_gt_imgid = tf.placeholder(tf.int32)
    feed_dict[f_gt_imgid] = feed_data.gt.imgid

    f_gt_speed = tf.placeholder(tf.float32)
    feed_dict[f_gt_speed] = feed_data.gt.speed

    f_pd_bbox = tf.placeholder(tf.float32)
    feed_dict[f_pd_bbox] = feed_data.pd.bbox

    f_pd_imgid = tf.placeholder(tf.int32)
    feed_dict[f_pd_imgid] = feed_data.pd.imgid

    f_pd_score = tf.placeholder(tf.float32)
    feed_dict[f_pd_score] = feed_data.pd.score

    num_gt_bboxes = feed_data.gt.imgid.shape[0]
    num_pd_bboxes = feed_data.pd.imgid.shape[0]
    gt_class_ids = tf.constant(classid, dtype=tf.uint8, shape=[num_gt_bboxes])
    pd_class_ids = tf.constant(classid, dtype=tf.uint8, shape=[num_pd_bboxes])
    ap, ap_ha, pr, pr_ha, _ = py_metrics_ops.detection_metrics(
        prediction_bbox=f_pd_bbox,
        prediction_type=pd_class_ids,
        prediction_score=f_pd_score,
        prediction_frame_id=tf.cast(f_pd_imgid, tf.int64),
        prediction_overlap_nlz=tf.zeros_like(f_pd_imgid, dtype=tf.bool),
        ground_truth_bbox=f_gt_bbox,
        ground_truth_type=gt_class_ids,
        ground_truth_frame_id=tf.cast(f_gt_imgid, tf.int64),
        ground_truth_difficulty=tf.zeros_like(f_gt_imgid, dtype=tf.uint8),
        ground_truth_speed=f_gt_speed,
        config=self._waymo_metric_config.SerializeToString())

    # All tensors returned by Waymo's metric op have a leading dimension
    # B=number of breakdowns. At this moment we always use B=1 to make
    # it compatible to the python code.
    scalar_metrics = {'ap': ap[0], 'ap_ha_weighted': ap_ha[0]}
    curve_metrics = {'pr': pr[0], 'pr_ha_weighted': pr_ha[0]}

    for i, metric in enumerate(breakdown_names):
      # There is a scalar / curve for every breakdown.
      scalar_metrics['ap_%s' % metric] = ap[i]
      scalar_metrics['ap_ha_weighted_%s' % metric] = ap_ha[i]
      curve_metrics['pr_%s' % metric] = pr[i]
      curve_metrics['pr_ha_weighted_%s' % metric] = pr_ha[i]
    return scalar_metrics, curve_metrics, feed_dict

  @property
  def value(self):
    """Returns weighted mAP over all eval classes."""
    self._EvaluateIfNecessary()
    ap = self._breakdown_metrics['waymo']._average_precisions  # pylint:disable=protected-access
    breakdown_names = config_util.get_breakdown_names_from_config(
        self._waymo_metric_config)

    num_sum = 0.0
    denom_sum = 0.0
    # Compute the average AP over all eval classes.  The first breakdown
    # is the overall mAP.
    for class_index in range(len(self.metadata.EvalClassIndices())):
      num_sum += np.nan_to_num(ap[breakdown_names[0]][class_index])
      denom_sum += 1.
    return num_sum / denom_sum

  def Summary(self, name):
    """Implements custom Summary for Waymo metrics."""
    self._EvaluateIfNecessary()

    ret = tf.Summary()
    # Put '.value' first (so it shows up in logs / summaries, etc).
    ret.value.add(tag='{}/weighted_mAP'.format(name), simple_value=self.value)

    ap = self._breakdown_metrics['waymo']._average_precisions  # pylint:disable=protected-access
    aph = self._breakdown_metrics['waymo']._average_precision_headings  # pylint:disable=protected-access
    breakdown_names = config_util.get_breakdown_names_from_config(
        self._waymo_metric_config)
    for i, j in enumerate(self.metadata.EvalClassIndices()):
      classname = self.metadata.ClassNames()[j]
      for k, breakdown_name in enumerate(breakdown_names):
        # Skip adding entries for breakdowns that are in a different class.
        #
        # The first breakdown is the overall one, so never skip it.
        if k > 0 and classname.lower() not in breakdown_name.lower():
          continue

        if k == 0:
          # For the overall mAP, include the class name
          # and set the breakdown_str to 'default' for backwards compatibility.
          prefix = '{}/{}'.format(name, classname)
          breakdown_str = 'default'
        else:
          # All breakdowns after the first one are extra and elide
          # the classname, since it is present in the breakdown_name.
          prefix = '{}_extra'.format(name)
          breakdown_str = breakdown_name

        tag_str = '{}/AP_{}'.format(prefix, breakdown_str)
        ap_value = ap[breakdown_name][i]
        ret.value.add(tag=tag_str, simple_value=ap_value)
        tag_str = '{}/APH_{}'.format(prefix, breakdown_str)
        aph_value = aph[breakdown_name][i]
        ret.value.add(tag=tag_str, simple_value=aph_value)

    image_summaries = self._breakdown_metrics['waymo'].GenerateSummaries(name)
    for image_summary in image_summaries:
      ret.value.extend(image_summary.value)

    return ret


class WaymoBreakdownMetric(breakdown_metric.BreakdownMetric):
  """Calculate average precision as function of difficulty."""

  @classmethod
  def Params(cls):
    p = super(WaymoBreakdownMetric, cls).Params()
    p.Define(
        'breakdown_list', [],
        'A list of breakdown names corresponding to the breakdown '
        'metrics computed from the Waymo breakdown generator config.')
    return p

  def __init__(self, p):
    super(WaymoBreakdownMetric, self).__init__(p)
    self._average_precision_headings = {}
    self._precision_recall_headings = {}

  def ComputeMetrics(self, compute_metrics_fn):
    p = self.params
    tf.logging.info('Calculating waymo AP breakdowns: start')
    scalars, curves = compute_metrics_fn()

    for breakdown_str in p.breakdown_list:
      self._average_precisions[breakdown_str] = [
          s['ap_%s' % breakdown_str] for s in scalars
      ]
      self._average_precision_headings[breakdown_str] = [
          s['ap_ha_weighted_%s' % breakdown_str] for s in scalars
      ]
      self._precision_recall[breakdown_str] = np.array(
          [c['pr_%s' % breakdown_str] for c in curves])
      self._precision_recall_headings[breakdown_str] = np.array(
          [c['pr_ha_weighted_%s' % breakdown_str] for c in curves])
    tf.logging.info('Calculating waymo AP breakdowns: finished')

  def GenerateSummaries(self, name):
    """Generate an image summary for precision recall by difficulty."""
    p = self.params

    image_summaries = []
    for i, j in enumerate(p.metadata.EvalClassIndices()):

      def _Setter(fig, axes):
        """Configure the plot for precision recall."""
        ticks = np.arange(0, 1.05, 0.1)
        axes.grid(b=False)
        axes.set_xlabel('Recall')
        axes.set_xticks(ticks)
        axes.set_ylabel('Precision')
        axes.set_yticks(ticks)
        # TODO(vrv): Add legend indicating number of objects in breakdown.
        fig.tight_layout()

      classname = p.metadata.ClassNames()[j]
      for k, breakdown_str in enumerate(p.breakdown_list):
        # Skip adding entries for breakdowns that are in a different class.
        #
        # The first breakdown is the overall one, so never skip it.
        if k > 0 and classname.lower() not in breakdown_str.lower():
          continue
        ps = [self._precision_recall[breakdown_str][i][:, 0]]
        rs = [self._precision_recall[breakdown_str][i][:, 1]]
        if k == 0:
          # Overall class PR.
          tag_str = '{}/{}/PR'.format(name, classname)
        else:
          tag_str = '{}/{}/{}/PR'.format(name, classname, breakdown_str)
        image_summary = plot.Curve(
            name=tag_str,
            figsize=(10, 8),
            xs=rs[0],
            ys=np.array(ps).T,
            setter=_Setter,
            marker='.',
            markersize=14,
            linestyle='-',
            linewidth=2,
            alpha=0.5)
        image_summaries.append(image_summary)

    return image_summaries

  # Fill in dummy implementations which are largely
  # unused.  The current implementation does not provide breakdown
  # image summaries that do bucketing; we assume that the waymo breakdown
  # implementations will break things down as necessary.
  def AccumulateHistogram(self, result):
    pass

  def AccumulateCumulative(self, result):
    pass

  def NumBinsOfHistogram(self):
    return 1
