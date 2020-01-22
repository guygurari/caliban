"""gke utility routines"""

from typing import Dict, List, Optional, Any
import logging
from urllib.parse import urlencode
from time import sleep
import re
import pprint as pp
from yaspin import yaspin
from yaspin.spinners import Spinners

import googleapiclient
from googleapiclient import discovery

import caliban.gke.constants as k
from caliban.gke.types import NodeImage, OpStatus
from caliban.cloud.types import (GPU, GPUSpec, TPU, TPUSpec)

# ----------------------------------------------------------------------------
def trap(error_value: Any, silent: bool = True) -> Any:
  """decorator that traps exceptions

  Args:
  error_value: value to return on error
  silent: do not log exceptions

  Returns:
  error_value on exception, function return value otherwise
  """

  def check(fn):

    def wrapper(*args, **kwargs):
      try:
        response = fn(*args, **kwargs)
      except Exception as e:
        if not silent:
          logging.exception(f'exception in call {fn}:\n{e}')
        return error_value
      return response

    return wrapper

  return check


# ----------------------------------------------------------------------------
def validate_gpu_spec_against_limits(
    gpu_spec: GPUSpec,
    gpu_limits: Dict[GPU, int],
    limit_type: str,
) -> bool:
  """validate gpu spec against provided limits

  Args:
  gpu_spec: gpu spec
  gpu_limits: limits
  limit_type: label for error messages

  Returns:
  True if spec is valid, False otherwise
  """

  if gpu_spec.gpu not in gpu_limits:
    logging.error(
        f'unsupported gpu type {gpu_spec.gpu.name}. ' +
        f'Supported types for {limit_type}: {[g.name for g in gpu_limits]}')
    return False

  if gpu_spec.count > gpu_limits[gpu_spec.gpu]:
    logging.error(
        f'error: requested {gpu_spec.gpu.name} gpu count {gpu_spec.count} unsupported,'
        + f' {limit_type} max = {gpu_limits[gpu_spec.gpu]}')
    return False

  return True


# ----------------------------------------------------------------------------
def nvidia_daemonset_url(node_image: NodeImage) -> Optional[str]:
  '''gets nvidia driver daemonset url for given node image

  Args:
  node_image: node image type

  Returns:
  daemonset yaml url on success, None otherwise
  '''

  DAEMONSETS = {
      NodeImage.COS: k.NVIDIA_DRIVER_COS_DAEMONSET_URL,
      NodeImage.UBUNTU: k.NVIDIA_DRIVER_UBUNTU_DAEMONSET_URL
  }

  return DAEMONSETS.get(node_image, None)


# ----------------------------------------------------------------------------
def dashboard_cluster_url(cluster_id: str, zone: str, project_id: str):
  """returns gcp dashboard url for given cluster

  Args:
  cluster_id: cluster name
  zone: zone string
  project_id: project name

  Returns:
  url string
  """

  query = urlencode({'project': project_id})
  return f'{k.DASHBOARD_CLUSTER_URL}/{zone}/{cluster_id}?{query}'


# ----------------------------------------------------------------------------
@trap(None)
def get_tpu_drivers(tpu_api: discovery.Resource, project_id: str,
                    zone: str) -> Optional[List[str]]:
  """gets supported tpu drivers for given project, zone

  Args:
  tpu_api: discovery tpu api resource
  project_id: project id
  zone: zone identifier

  Returns:
  list of supported drivers on success, None otherwise
  """

  location = 'projects/' + project_id + '/locations/' + zone

  rsp = tpu_api.projects().locations().tensorflowVersions().list(
      parent=location).execute()

  if rsp is None:
    logging.error('error getting tpu drivers')
    return None

  return [d['version'] for d in rsp['tensorflowVersions']]


# ----------------------------------------------------------------------------
def user_verify(msg: str, default: bool) -> bool:
  """prompts user to verify a choice

  Args:
  msg: message to display to user
  default: default value if user simply hit 'return'

  Returns:
  boolean choice
  """
  choice_str = '[Yn]' if default else '[yN]'

  while True:
    ok = input(f'\n {msg} {choice_str}: ').lower()

    if len(ok) == 0:
      return default

    if ok not in ['y', 'n']:
      print('please enter y or n')
      continue

    return (ok == 'y')

  return False


# ----------------------------------------------------------------------------
@trap(None)
def wait_for_operation(cluster_api: discovery.Resource,
                       name: str,
                       conditions: List[OpStatus] = [
                           OpStatus.DONE, OpStatus.ABORTING
                       ],
                       sleep_sec: int = 1,
                       message: str = '',
                       spinner: bool = True) -> Optional[dict]:
  """waits for cluster operation to reach given state(s)

  Args:
  cluster_api: cluster api client
  name: operation name, of form projects/*/locations/*/operations/*
  conditions: exit status conditions
  sleep_sec: polling interval
  message: wait message
  spinner: display spinner while waiting

  Returns:
  response dictionary on success, None otherwise
  """

  if len(conditions) == 0:
    return None

  condition_strings = [x.name for x in conditions]

  def _wait():
    while True:
      rsp = cluster_api.projects().locations().operations().get(
          name=name).execute()

      if rsp['status'] in condition_strings:
        return rsp

      sleep(sleep_sec)
    return None

  if spinner:
    with yaspin(Spinners.line, text=message) as spinner:
      return _wait()

  return _wait()


# ----------------------------------------------------------------------------
@trap(None)
def gke_tpu_to_tpuspec(tpu: str) -> Optional[TPUSpec]:
  """convert gke tpu accelerator string to TPUSpec

  Args:
  tpu: gke tpu string

  Returns:
  TPUSpec on success, None otherwise
  """

  tpu_re = re.compile('^(?P<tpu>(v2|v3))-(?P<count>[0-9]+)$')
  gd = tpu_re.match(tpu).groupdict()

  return TPUSpec(TPU[gd['tpu'].upper()], int(gd['count']))

# ----------------------------------------------------------------------------
@trap(None)
def get_zone_tpu_types(tpu_api: discovery.Resource, project_id: str,
                       zone: str) -> Optional[List[TPUSpec]]:
  """gets list of tpus available in given zone

  Args:
  tpu_api: tpu api instance
  project_id: project id
  zone: zone string

  Returns:
  list of supported tpu specs on success, None otherwise
  """

  location = f'projects/{project_id}/locations/{zone}'
  rsp = tpu_api.projects().locations().acceleratorTypes().list(
      parent=location).execute()

  tpus = []
  for t in rsp['acceleratorTypes']:
    spec = gke_tpu_to_tpuspec(t['type'])
    if spec is None:
      continue
    tpus.append(spec)

  return tpus


# ----------------------------------------------------------------------------
@trap(None)
def gke_gpu_to_gpu(gpu: str) -> Optional[GPU]:
  """convert gke gpu string to GPU type

  Args:
  gpu: gke gpu string

  Returns:
  GPU on success, None otherwise
  """

  gpu_re = re.compile('^nvidia-tesla-(?P<type>[a-z0-9]+)$')
  gd = gpu_re.match(gpu).groupdict()
  return GPU[gd['type'].upper()]


# ----------------------------------------------------------------------------
@trap(None)
def get_zone_gpu_types(
    project_id: str, zone: str,
    compute_api: discovery.Resource) -> Optional[List[GPUSpec]]:
  """gets list of gpu accelerators available in given zone

  Args:
  project_id: project id
  zone: zone string
  compute_api: compute api instance

  Returns:
  list of GPUSpec on success (count is max count), None otherwise
  """

  rsp = compute_api.acceleratorTypes().list(
      project=project_id, zone=zone).execute()

  gpus = []

  for x in rsp['items']:
    gpu = gke_gpu_to_gpu(x['name'])
    if gpu is None:
      continue
    gpus.append(GPUSpec(gpu, int(x['maximumCardsPerInstance'])))

  return gpus


# ----------------------------------------------------------------------------
@trap(None)
def get_region_quotas(
    project_id: str, region: str,
    compute_api: discovery.Resource) -> Optional[List[Dict[str, Any]]]:
  """gets compute quotas for given region

  These quotas include cpu and gpu quotas for the given region.
  (tpu quotas are not included here)

  Args:
  project_id: project id
  region: region string
  compute_api: compute_api instance

  Returns:
  list of quota dicts, with keys {'limit', 'metric', 'usage'}, None on error
  """

  return compute_api.regions().get(
      project=project_id, region=region).execute().get('quotas', [])


# ----------------------------------------------------------------------------
@trap(None)
def resource_limits_from_quotas(
    quotas: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
  """create resource limits from quota dictionary

  Args:
  quotas: list of quota dicts, with keys {'limit', 'metric', 'usage'}

  Returns:
  resource limits dictionaries on success, None otherwise
  """

  limits = []

  gpu_re = re.compile('^NVIDIA_(?P<gpu>[A-Z0-9]+)_GPUS$')

  for q in quotas:
    metric = q['metric']
    limit = q['limit']

    if metric == 'CPUS':
      limits.append({'resourceType': 'cpu', 'maximum': str(limit)})
      limits.append({
          'resourceType': 'memory',
          'maximum': str(int(limit) * k.MAX_GB_PER_CPU)
      })
      continue

    gpu_match = gpu_re.match(metric)
    if gpu_match is None:
      continue

    gd = gpu_match.groupdict()
    gpu_type = gd['gpu']

    limits.append({
        'resourceType': f'nvidia-tesla-{gpu_type.lower()}',
        'maximum': str(limit)
    })

  return limits

# ----------------------------------------------------------------------------
@trap(None)
def generate_resource_limits(
    project_id: str, region: str,
    compute_api: discovery.Resource) -> Optional[List[Dict[str, Any]]]:
  """generates resource limits from quota information

  Args:
  project_id: project id
  region: region string
  compute_api: compute_api instance

  Returns:
  resource limits dictionaries on success, None otherwise
  """

  quotas = get_region_quotas(project_id, region, compute_api)
  if quotas is None:
    return None

  return resource_limits_from_quotas(quotas)