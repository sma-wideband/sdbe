'''
 simple Queue based workflow using CUDA kernels
 Based on rslib.py script by LLB
 Steps implemented:
  - 
'''

import sys
sdbe_scripts_dir = '/home/krosenfe/sdbe/software/prototyping'
sys.path.append(sdbe_scripts_dir)

import pycuda.autoinit
import pycuda.driver as cuda
from pycuda.compiler import SourceModule
import scikits.cuda.cufft as cufft
from kernel_template import kernel_template

from threading import Thread
from Queue import Queue
from numpy.fft import rfft, irfft
from numpy import complex64,float32,float64,int32,uint32,array,arange,empty,zeros,ceil,roll
from numpy import min,floor,ceil,exp,angle,reshape,sqrt,roll,log2
from argparse import ArgumentParser
from struct import unpack
import logging
from timeit import default_timer as timer
from collections import defaultdict

import read_r2dbe_vdif,cross_corr,sdbe_preprocess
import h5py

VDIF_BYTE_SIZE = 1056
VDIF_BYTE_SIZE_DATA = 1024
VDIF_INT_SIZE = VDIF_BYTE_SIZE/4
VDIF_PER_BENG = 2048

BENG_CHANNELS_ = 16384
BENG_CHANNELS = (BENG_CHANNELS_ + 1)
BENG_SNAPSHOTS = 128

SWARM_RATE = 2496e6
R2DBE_RATE = 4096e6

BENG_BUFFER_IN_COUNTS = 40
SNAPSHOTS_PER_BATCH = 39

class sdbe_cupreprocess(object): 

  def __init__(self,gpuid,resamp_kind='linear'):
    #self.ctx = cuda.Device(gpuid).make_context()
    #self.device = self.ctx.get_device()

    #self.device = cuda.Device(gpuid)
    #self.ctx = self.device.make_context()

    # basic info   
    self.gpuid = gpuid
    self.num_beng_counts = BENG_BUFFER_IN_COUNTS-1
    self.num_swarm_samples = int((BENG_BUFFER_IN_COUNTS-1)*BENG_SNAPSHOTS*2*BENG_CHANNELS_)
    self.num_r2dbe_samples = int(self.num_swarm_samples*R2DBE_RATE/SWARM_RATE)
    self.__resamp_kind = resamp_kind
    # grab GPU
    # compile CUDA kernels
    kernel_source = kernel_template % {'BENG_BUFFER_IN_COUNTS':BENG_BUFFER_IN_COUNTS}
    kernel_module = SourceModule(kernel_source)
    self.__vdif_to_beng = kernel_module.get_function('vdif_to_beng')
    self.__reorderTz_smem = kernel_module.get_function('reorderTz_smem')
    self.__linear_interp = kernel_module.get_function('linear')


    # initalize FFT plans:
    if self.__resamp_kind is 'linear':
      # inverse in-place FFT plan
      n = array([2*BENG_CHANNELS_],int32)
      inembed = array([BENG_CHANNELS],int32)
      onembed = array([2*BENG_CHANNELS],int32)
      self.__plan_A = cufft.cufftPlanMany(1, n.ctypes.data, inembed.ctypes.data, 1, BENG_CHANNELS,
	                                       onembed.ctypes.data, 1, 2*BENG_CHANNELS,
  					       cufft.CUFFT_C2R, self.num_beng_counts*BENG_SNAPSHOTS)
      # band trimming R2C FFT plan
      n = array([4096],int32)
      inembed = array([4096],int32)
      onembed = array([4096/2+1],int32)
      self.__bandlimit_batch = self.num_r2dbe_samples / 4096 / 128 # we are memory limited.
      self.__plan_B = cufft.cufftPlanMany(1, n.ctypes.data, inembed.ctypes.data, 1, 4096,
					       onembed.ctypes.data, 1, 4096/2+1,
					       cufft.CUFFT_R2C, self.__bandlimit_batch)
      # band trimming C2R FFT plan
      n = array([2048],int32)
      inembed = array([2048/2+1],int32)
      onembed = array([2048],int32)
      self.__plan_C = cufft.cufftPlanMany(1, n.ctypes.data, inembed.ctypes.data, 1, 4096/2+1,
					       onembed.ctypes.data, 1, 2048,
					       cufft.CUFFT_C2R, self.__bandlimit_batch)
    elif self.__resamp_kind is 'fft':
      # inverse FFT plan
      n = array([2*BENG_CHANNELS_],int32)
      inembed = array([BENG_CHANNELS],int32)
      onembed = array([2*BENG_CHANNELS_],int32)
      self.__plan_A = cufft.cufftPlanMany(1, n.ctypes.data, inembed.ctypes.data, 1, BENG_CHANNELS,
                                      onembed.ctypes.data, 1, 2*BENG_CHANNELS_,
 				       cufft.CUFFT_C2R, self.num_beng_counts*BENG_SNAPSHOTS)

      self.__bandlimit_batch = 32
      # Turn concatenated SWARM time series into single spectrum
      n = array([39*2*BENG_CHANNELS_],int32)
      inembed = array([39*2*BENG_CHANNELS_],int32)
      onembed = array([39*BENG_CHANNELS_+1],int32)
      self.__plan_B = cufft.cufftPlanMany(1,n.ctypes.data,
					inembed.ctypes.data,1,39*2*BENG_CHANNELS_,
					onembed.ctypes.data,1,39*BENG_CHANNELS_+1,
					cufft.CUFFT_R2C,self.__bandlimit_batch)

      # Turn trimmed spectrum into 2048 timeseries
      n = array([32*2*BENG_CHANNELS_],int32)
      inembed = array([39*BENG_CHANNELS_+1],int32)
      onembed = array([32*2*BENG_CHANNELS_],int32)
      self.__plan_C = cufft.cufftPlanMany(1,n.ctypes.data,
					inembed.ctypes.data,1,39*BENG_CHANNELS_+1,
					onembed.ctypes.data,1,32*2*BENG_CHANNELS_,
					cufft.CUFFT_C2R,self.__bandlimit_batch)

    self.__gpu_vdif_buf = None
    self.__gpu_beng_data_0 = None
    self.__gpu_beng_data_1 = None
    self.__gpu_beng_0 = None
    self.__gpu_beng_1 = None
    self.__gpu_r2dbe = None


  def memcpy_sdbe_vdif(self,cpu_vdif_buf,bcount_offset):
    ''' Move sdbe vdif buffer buffer to device.'''
    self.bcount_offset = bcount_offset
    self.__gpu_vdif_buf = cuda.mem_alloc(cpu_vdif_buf.nbytes)
    cuda.memcpy_htod(self.__gpu_vdif_buf,cpu_vdif_buf)

  def depacketize_sdbe_vdif(self,blocks_per_grid=128):
    ''' 
    depacketize sdbe vdif:
    consecutive snapshots are indexed fastest 
    '''
    logger.debug('depacketizing B-engine data')
    self.__gpu_beng_data_0 = cuda.mem_alloc(8*BENG_CHANNELS_*BENG_SNAPSHOTS*BENG_BUFFER_IN_COUNTS)
    self.__gpu_beng_data_1 = cuda.mem_alloc(8*BENG_CHANNELS_*BENG_SNAPSHOTS*BENG_BUFFER_IN_COUNTS)
    gpu_fid         = cuda.mem_alloc(4*VDIF_PER_BENG*BENG_BUFFER_IN_COUNTS)
    gpu_cid         = cuda.mem_alloc(4*VDIF_PER_BENG*BENG_BUFFER_IN_COUNTS)
    gpu_bcount      = cuda.mem_alloc(4*VDIF_PER_BENG*BENG_BUFFER_IN_COUNTS)
    gpu_beng_frame_completion = cuda.mem_alloc(4*BENG_BUFFER_IN_COUNTS)
    # launch kernel
    self.__vdif_to_beng(self.__gpu_vdif_buf,
			gpu_fid, 
			gpu_cid, 
			gpu_bcount,
			self.__gpu_beng_data_0,
			self.__gpu_beng_data_1,
			gpu_beng_frame_completion,
			int32(BENG_BUFFER_IN_COUNTS*VDIF_PER_BENG),
			int32(self.bcount_offset),
  			block=(32,32,1), grid=(blocks_per_grid,1,1))
    # we might need these later, but let's free the memory for now.
    gpu_fid.free()
    gpu_cid.free()
    gpu_bcount.free()
    gpu_beng_frame_completion.free()
    # free 2-bit vdif 
    self.__gpu_vdif_buf.free()

  def reorder_beng_data(self):
    '''
    reorder B-engine data with a shift by 2 snapshots where (channel index / 4) is even
    and then by 1 B-frame where snapshot index > 68.
    '''
    logger.debug('reordering B-engine data')
    self.__gpu_beng_0 = cuda.mem_alloc(8*BENG_CHANNELS*BENG_SNAPSHOTS*(BENG_BUFFER_IN_COUNTS-1))
    self.__gpu_beng_1 = cuda.mem_alloc(8*BENG_CHANNELS*BENG_SNAPSHOTS*(BENG_BUFFER_IN_COUNTS-1))
    self.__reorderTz_smem(self.__gpu_beng_data_0,self.__gpu_beng_0,int32(BENG_BUFFER_IN_COUNTS),
		block=(16,16,1),grid=(BENG_CHANNELS_*BENG_SNAPSHOTS/(16*16),1,1),)
    self.__reorderTz_smem(self.__gpu_beng_data_1,self.__gpu_beng_1,int32(BENG_BUFFER_IN_COUNTS),
		block=(16,16,1),grid=(BENG_CHANNELS_*BENG_SNAPSHOTS/(16*16),1,1),)
    # free unpacked, unordered b-frames
    self.__gpu_beng_data_0.free()
    self.__gpu_beng_data_1.free()

  def resamp(self):
    if self.__resamp_kind is 'linear':
      self.__fft_linear_interp()
    elif self.__resamp_kind is 'fft':
      self.__fft_resample()

  def __fft_resample(self):
    '''
    Resample using FFTs.
    Requires that use_fft_resample flag is True.
    __gpu_r2dbe has both phased sums resampled at 2048 MHz. 
    '''
    logger.debug('Resampling using FFTs')
    self.__gpu_r2dbe = cuda.mem_alloc(4 * self.num_r2dbe_samples)
    gpu_swarm = cuda.mem_alloc(4 * self.num_swarm_samples)
    # loop over phased sums
    phase_sum_i = 0
    for SB in (self.__gpu_beng_0, self.__gpu_beng_1):
      cufft.cufftExecC2R(self.__plan_A,int(SB),int(gpu_swarm))
      SB.free()
 
      gpu_tmp = cuda.mem_alloc(8*int(39*BENG_CHANNELS_+1)*self.__bandlimit_batch)
      for ib in range((BENG_BUFFER_IN_COUNTS-1)*BENG_SNAPSHOTS/39/self.__bandlimit_batch):
        # Turn concatenated SWARM time series into single spectrum
        cufft.cufftExecR2C(self.__plan_B,int(gpu_swarm)+
						int(4*39*2*BENG_CHANNELS_*self.__bandlimit_batch*ib),int(gpu_tmp))
        # Turn padded SWARM spectrum into time series with R2DBE sampling rate
        cufft.cufftExecC2R(self.__plan_C,
			int(gpu_tmp)+int(8*150*512),int(self.__gpu_r2dbe)+
						int(4*32*2*BENG_CHANNELS_*ib*self.__bandlimit_batch)+
						int(4*phase_sum_i*self.num_r2dbe_samples/2))
      gpu_tmp.free()
      phase_sum_i += 1

    gpu_swarm.free()

  def __fft_linear_interp(self):
    '''
    Resample using linear interpolation.
    '''
    logger.debug('Resampling using linear interpolation')
    threads_per_block = 512
    blocks_per_grid = int(ceil(1. * self.num_r2dbe_samples / threads_per_block))

    # allocate device memory
    self.__gpu_r2dbe = cuda.mem_alloc(4 * self.num_r2dbe_samples) # 25% of device memory
    gpu_r2dbe_spec = cuda.mem_alloc(8 * (4096/2+1) * self.__bandlimit_batch) # memory peanuts

    phase_sum_i = 0
    for SB in (self.__gpu_beng_0, self.__gpu_beng_1):
      # Turn SWARM snapshots into timeseries
      cufft.cufftExecC2R(self.__plan_A,int(SB),int(SB))
      # resample 
      gpu_resamp = cuda.mem_alloc(4 * self.num_r2dbe_samples) # 25% of device memory
      self.__linear_interp(SB,
		int32(self.num_swarm_samples),
		gpu_resamp,
		int32(self.num_r2dbe_samples),
		float64(SWARM_RATE/R2DBE_RATE),
		float32(1./(2*BENG_CHANNELS_)),
		block=(threads_per_block,1,1),grid=(blocks_per_grid,1))
      SB.free()

      # loop through resampled time series in chunks of batch_B num_r2dbe_samples/4096/__bandlimit_batch
      for ib in range(self.num_r2dbe_samples/4096/self.__bandlimit_batch):
        # compute spectrum with Fs = 4096
        cufft.cufftExecR2C(self.__plan_B,
        int(gpu_resamp)+int(4*ib*4096*self.__bandlimit_batch),int(gpu_r2dbe_spec))
        # invert to time series with B=1024, masking out first 150 MHz and last (1024-150) MHz.(MUST INDEX BY 8)
        cufft.cufftExecC2R(self.__plan_C,
		int(gpu_r2dbe_spec)+int(8*150),
		int(self.__gpu_r2dbe) + 
		int(4*ib*2048*self.__bandlimit_batch) + 
		int(4*phase_sum_i*self.num_r2dbe_samples/2))

      gpu_resamp.free()
      phase_sum_i += 1

    gpu_r2dbe_spec.free()

  def gpumeminfo(self,driver):
    logger.info('Memory usage: %f' % (1.- 1.*driver.mem_get_info()[0]/driver.mem_get_info()[1]))

  def cleanup(self):

    # destroy plans
    cufft.cufftDestroy(self.__plan_A)
    cufft.cufftDestroy(self.__plan_B)
    cufft.cufftDestroy(self.__plan_C)

    #
    self.__gpu_r2dbe.free()

    # delete context
    #self.ctx.pop()
    #del self.ctx

#################################################################

def get_bcount_from_vdif(vdif_start):
  '''
  read .vdif file into uint32 word array 
  '''
  BENG_VDIF_HDR_0_OFFSET_INT = 4  # b1 b2 b3 b4
  BENG_VDIF_HDR_1_OFFSET_INT = 5  #  c  0  f b0
  return ((vdif_start[BENG_VDIF_HDR_1_OFFSET_INT]&0xFF000000)>>24) + ((vdif_start[BENG_VDIF_HDR_0_OFFSET_INT]&0x00FFFFFF)<<8);

def read_sdbe_vdif(filename_input,num_vdif_frames,beng_frame_offset=1,batched=True):
  logger = logging.getLogger(__name__)
  vdif_buf = empty(VDIF_BYTE_SIZE*num_vdif_frames/4, dtype=uint32)
  for ii in arange(4):
    filename_data = "%s_swarmdbe_eth%d.vdif" % (filename_input, 2+ii)
    fh = open(filename_data,"r")
    # seek to specified B-engine frame boundary
    tmp_vdif = array(unpack('<{0}I'.format(VDIF_INT_SIZE),fh.read(VDIF_BYTE_SIZE)), uint32)
    tmp_bcount_curr = get_bcount_from_vdif(tmp_vdif)
    tmp_bcount_prev = tmp_bcount_curr
    while (tmp_bcount_curr-tmp_bcount_prev < beng_frame_offset):
      tmp_vdif = array(unpack('<{0}I'.format(VDIF_INT_SIZE),fh.read(VDIF_BYTE_SIZE)), uint32)
      tmp_bcount_curr = get_bcount_from_vdif(tmp_vdif)
    logger.debug('B-count = %d = %d, seeking one frame back' % (tmp_bcount_curr, tmp_bcount_prev+beng_frame_offset))
    fh.seek(-1*VDIF_BYTE_SIZE,1)
    bcount_offset = tmp_bcount_curr
    # read data
    vdif_buf[ii*VDIF_INT_SIZE*num_vdif_frames/4:(ii+1)*VDIF_INT_SIZE*num_vdif_frames/4] = array(unpack('<{0}I'.format(VDIF_INT_SIZE*num_vdif_frames/4),fh.read(VDIF_BYTE_SIZE * num_vdif_frames / 4)), uint32)
    fh.seek(-1*VDIF_BYTE_SIZE,1)
    tmp_vdif = array(unpack('<{0}I'.format(VDIF_INT_SIZE),fh.read(VDIF_BYTE_SIZE)), uint32)
    logger.debug('ending with B-count = %d, read %d B-engine frames' % (get_bcount_from_vdif(tmp_vdif), get_bcount_from_vdif(tmp_vdif)-bcount_offset+1))
    fh.close()
  return vdif_buf,bcount_offset

#################################################################

if __name__ == "__main__":

  #cuda.init()

  parser = ArgumentParser(description="Convert SWARM vdif data into 4096 Msps time-domain vdif data")
  parser.add_argument('-v', dest='verbose', action='store_true', help='display debug information')
  parser.add_argument('-b', dest='basename', help='scan filename base for SWARM data', default='prep6_test1_local')
  parser.add_argument('-d', dest='dir', help='directory for input SWARM data products', default='/home/shared/sdbe_preprocessed/')
  parser.add_argument('-s', dest='skip', type=int, help='starting snapshot', default=1)
  parser.add_argument('-n', dest='num_gpu', type=int, help='number of gpus to use',
		default=1,choices=range(1,cuda.Device.count()+1))
  parser.add_argument('-c', dest='correlate', action='store_true', help='correlate against R2DBE')
  args = parser.parse_args()

  logger = logging.getLogger(__name__)
  logger.addHandler(logging.StreamHandler())
  logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)


  # timers
  clock = defaultdict(float)
  counts = defaultdict(int)
  total = defaultdict(float)
  def tic(name):
    clock[name] = timer()
  def toc(name):
    counts[name] = counts[name] + 1
    total[name] = total[name] + timer() - clock[name]

  # I/O and prior processing
  rel_path_dat = args.dir
  scan_filename_base = args.basename

  if True: 

    # initalize GPUs (up to 4, not implemented yet.)
    gpus = []  
    #for g in range(args.num_gpu):
    for g in range(1):
      #gpus.append(sdbe_cupreprocess(g,resamp_kind='fft'))
      gpus.append(sdbe_cupreprocess(g,resamp_kind='linear'))
  
    # load vdif onto device
    for g in gpus:
      tic('python read')
      cpu_vdif_buf,bcount_offset = read_sdbe_vdif(rel_path_dat+scan_filename_base,BENG_BUFFER_IN_COUNTS*VDIF_PER_BENG,
						beng_frame_offset=g.gpuid*BENG_BUFFER_IN_COUNTS+1)
      toc('python read')
      tic('htod      ')
      g.memcpy_sdbe_vdif(cpu_vdif_buf,bcount_offset)
      toc('htod      ')
  
    tic('gpu total')
  
    # depacketize vdif
    tic('depacketize')
    for g in gpus:
      g.depacketize_sdbe_vdif()
    toc('depacketize')
  
    # reorder B-engine data
    tic('reorder  ')
    for g in gpus:
      g.reorder_beng_data()
    toc('reorder  ')
  
    # linear resampling
    tic('resample')
    for g in gpus:
      g.resamp()
    toc('resample')
  
    # To do: quantize and pack vdif
  
    toc('gpu total')
  
    # pull data from device
    for g in gpus:
      cpu_r2dbe = empty((2,g.num_r2dbe_samples/2),dtype=float32) # empty array for result
      cuda.memcpy_dtoh(cpu_r2dbe,g._sdbe_cupreprocess__gpu_r2dbe)

  # clean up
  for g in gpus:
    g.cleanup()
  
  # report timing info
  real_time = (BENG_BUFFER_IN_COUNTS-1)*BENG_SNAPSHOTS*2*BENG_CHANNELS_/SWARM_RATE
  print "%s\t%s\t%s\t%s" % ('operation', 'counts', 'total [ms]', 'x(real = %.3f)' % (real_time*1e3))
  print "-----------------------------------------------------------"
  for k in total.keys():
      print "%s\t%d\t%.2f\t\t%.3f" % (k, counts[k], 1e3*total[k], real_time/total[k]  )

  logger.debug('done!')

  if args.correlate:
    import matplotlib.pyplot as plt
    logger.info("INFO:: Correlating result again R2DBE")
  
    # Now read R2DBE data covering roughly the same time window as the SWARM
    # data. Start at an offset of zero (i.e. from the first VDIF packet) to
    # keep things simple.
    N_r_vdif_frames = int(ceil(BENG_SNAPSHOTS*(BENG_BUFFER_IN_COUNTS-1)*R2DBE_RATE/SWARM_RATE))
    vdif_frames_offset = 0
    rel_path_to_in = args.dir
    d = sdbe_preprocess.get_diagnostics_from_file(scan_filename_base,rel_path=rel_path_to_in)
    xr = read_r2dbe_vdif.read_from_file(rel_path_to_in + scan_filename_base + '_r2dbe_eth3.vdif',N_r_vdif_frames,vdif_frames_offset)
  
  
    # compare to pre-shifted data
    hf5 = h5py.File(rel_path_to_in + scan_filename_base + '_sdbe_preprocess.hdf5')
    spectra = hf5.get('Xs1').value
    xs_shifted = sdbe_preprocess.resample_sdbe_to_r2dbe_fft_interp(spectra,interp_kind="linear")
  
    # now we check the band limited series
    xr_bl = sdbe_preprocess.bandlimit_1248_to_1024(xr[:(xr.size//4096)*4096],sub_sample=True)
    xs_bl = sdbe_preprocess.bandlimit_1248_to_1024(xs_shifted[:(xs_shifted.size//4096)*4096],sub_sample=True)
  
    # check correlation given pre-determined time shift
    s_range = arange(-16,16)
    s_avg = 128
    offset_swarmdbe_data = d['offset_swarmdbe_data']
    idx_offset = d['get_idx_offset'][0]
    fft_window_size = 32768
  
    # FFT resampled
    x0 = xr_bl.copy()
    x1 = cpu_r2dbe[1,floor(idx_offset*2*BENG_CHANNELS_/SWARM_RATE*2048e6)+offset_swarmdbe_data/2-1:]
    n = min((2**int(floor(log2(x0.size))),2**int(floor(log2(x1.size)))))
    x0 = x0[:n]
    x1 = x1[:n]
  
    X0 = rfft(x0.reshape(n/fft_window_size,fft_window_size),axis=-1)
    X1 = rfft(x1.reshape(n/fft_window_size,fft_window_size),axis=-1)
    X0*= exp(-1.0j*angle(X0[0,0]))
    X1*= exp(-1.0j*angle(X1[0,0]))
  
    S_0x0 = (X0 * X0.conj()).mean(axis=0)
    S_1x1 = (X1 * X1.conj()).mean(axis=0)
    S_0x1 = (X0 * X1.conj()).mean(axis=0)
    p = 8; q = 16
    s_0x0 = irfft(S_0x0,n=p*fft_window_size).real
    s_1x1 = irfft(S_1x1,n=p*fft_window_size).real
    s_0x1 = irfft(S_0x1,n=p*fft_window_size).real/sqrt(s_0x0.max()*s_1x1.max())
  
    print 'resample:\t\t', s_0x1.max(), s_0x1.min()
    plt.figure()
    plt.stem(arange(-p*q/2,p*q/2),roll(s_0x1,p*q/2)[:p*q])
  
    #################
  
    x0 = xr_bl.copy()
    x1 = xs_bl[offset_swarmdbe_data/2-1:]
    n = min((2**int(floor(log2(x0.size))),2**int(floor(log2(x1.size)))))
    x0 = x0[:n]
    x1 = x1[:n]
  
    X0 = rfft(x0.reshape(n/fft_window_size,fft_window_size),axis=-1)
    X1 = rfft(x1.reshape(n/fft_window_size,fft_window_size),axis=-1)
    X0*= exp(-1.0j*angle(X0[0,0]))
    X1*= exp(-1.0j*angle(X1[0,0]))
  
    S_0x0 = (X0 * X0.conj()).mean(axis=0)
    S_1x1 = (X1 * X1.conj()).mean(axis=0)
    S_0x1 = (X0 * X1.conj()).mean(axis=0)
    p = 8; q = 16
    s_0x0 = irfft(S_0x0,n=p*fft_window_size).real
    s_1x1 = irfft(S_1x1,n=p*fft_window_size).real
    s_0x1 = irfft(S_0x1,n=p*fft_window_size).real/sqrt(s_0x0.max()*s_1x1.max())
    print 'linear+shift:\t\t', s_0x1.max(), s_0x1.min()
  
    plt.figure()
    plt.stem(arange(-p*q/2,p*q/2),roll(s_0x1,p*q/2)[:p*q])
  
    plt.show()
