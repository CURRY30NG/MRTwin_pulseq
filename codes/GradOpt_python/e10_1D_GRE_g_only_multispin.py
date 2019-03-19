#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
Created on Tue Jan 29 14:38:26 2019

@author: mzaiss

experiment desciption:
optimize for flip and gradient events and also for time delays between those
assume irregular event grid where flip and gradient events are interleaved with
relaxation and free pression events subject to free variable (dt) that specifies
the duration of each relax/precess event
__allow for magnetization transfer over repetitions__

1D imaging: 


"""

import os, sys
import numpy as np
import scipy
import torch
import cv2
import matplotlib.pyplot as plt
from torch import optim

import core.spins
import core.scanner
import core.opt_helper
import core.target_seq_holder

if sys.version_info[0] < 3:
    reload(core.spins)
    reload(core.scanner)
    reload(core.opt_helper)
else:
    import importlib
    importlib.reload(core.spins)
    importlib.reload(core.scanner)
    importlib.reload(core.opt_helper)    

use_gpu = 1

# NRMSE error function
def e(gt,x):
    return 100*np.linalg.norm((gt-x).ravel())/np.linalg.norm(gt.ravel())
    
# torch to numpy
def tonumpy(x):
    return x.detach().cpu().numpy()

# get magnitude image
def magimg(x):
  return np.sqrt(np.sum(np.abs(x)**2,2))

# device setter
def setdevice(x):
    if use_gpu:
        x = x.cuda(0)
        
    return x
    
def imshow(x, title=None):
    ax = plt.imshow(x, interpolation='none')
    if title != None:
        plt.title(title)
    plt.ion()
    fig = plt.gcf()
    fig.colorbar(ax)
    fig.set_size_inches(1, 1)
    plt.show()     

def stop():
    sys.tracebacklimit = 0
    class ExecutionControl(Exception): pass
    raise ExecutionControl('stopped by user')
    sys.tracebacklimit = 1000

# define setup
sz = np.array([32,32])                                           # image size
NRep = 32                                          # number of repetitions
T = sz[0] + 3                                        # number of events F/R/P
NSpins = 50                                # number of spin sims in each voxel
NCoils = 1                                  # number of receive coil elements
#dt = 0.0001                         # time interval between actions (seconds)

noise_std = 0*1e-2                               # additive Gaussian noise std

NVox = sz[0]*sz[1]


#############################################################################
## Init spin system and the scanner ::: #####################################

    # initialize scanned object
spins = core.spins.SpinSystem(sz,NVox,NSpins,use_gpu)

numerical_phantom = np.ones((sz[0],sz[1],3))*0.01
numerical_phantom[10,:,:]=2
numerical_phantom[23,:,:]=1
numerical_phantom[24,:,:]=1.5
numerical_phantom[25,:,:]=1
numerical_phantom[30,:,:]=0.1
numerical_phantom[28,:,:]=0.3
numerical_phantom[29,:,:]=0.6
numerical_phantom[30,:,:]=0.3
numerical_phantom[31,:,:]=0.1
numerical_phantom[:,:,2]*=1000.1  # T2=100ms

numerical_phantom = np.load('../../data/brainphantom_2D.npy')
numerical_phantom = cv2.resize(numerical_phantom, dsize=(sz[0],sz[0]), interpolation=cv2.INTER_CUBIC)

#numerical_phantom = cv2.resize(numerical_phantom, dsize=(sz[0], sz[1]), interpolation=cv2.INTER_CUBIC)
numerical_phantom[numerical_phantom < 0] = 0

spins.set_system(numerical_phantom)

cutoff = 1e-12
spins.T1[spins.T1<cutoff] = cutoff
spins.T2[spins.T2<cutoff] = cutoff


R2 = 100.0
omega = np.linspace(0+1e-5,1-1e-5,NSpins) - 0.5
#omega = np.random.rand(NSpins,NVox) - 0.5
omega = np.expand_dims(omega,1).repeat(NVox, axis=1)
#omega = np.expand_dims(omega[:,0],1).repeat(NVox, axis=1)
omega = R2 * np.tan ( np.pi  * omega)

#mega = np.random.rand(NSpins,NVox) * 1000

spins.omega = torch.from_numpy(omega.reshape([NSpins,NVox])).float()
spins.omega[torch.abs(spins.omega) > 1e3] = 0
spins.omega = setdevice(spins.omega)


scanner = core.scanner.Scanner_fast(sz,NVox,NSpins,NRep,T,NCoils,noise_std,use_gpu)
scanner.set_adc_mask()

# allow for relaxation after last readout event
scanner.adc_mask[:scanner.T-scanner.sz[0]-1] = 0
scanner.adc_mask[-1] = 0

# init tensors
flips = torch.ones((T,NRep), dtype=torch.float32) * 0 * np.pi/180
flips[0,:] = 90*np.pi/180  # GRE preparation part 1 : 90 degree excitation
#flips[0,1] = 0
     
flips = setdevice(flips)
     
scanner.init_flip_tensor_holder()
scanner.set_flip_tensor(flips)

# gradient-driver precession
# Cartesian encoding
grad_moms = torch.zeros((T,NRep,2), dtype=torch.float32) 

grad_moms[T-sz[0]-1:-1,:,0] = torch.linspace(-int(sz[0]/2),int(sz[0]/2)-1,int(sz[0])).view(int(sz[0]),1).repeat([1,NRep])
#grad_moms[T-sz[0]-1:-1,:,1] = torch.linspace(-int(sz[1]/2),int(sz[1]/2-1),int(NRep)).repeat([sz[0],1])
if NRep == 1:
    grad_moms[T-sz[0]-1:-1,:,1] = torch.zeros((1,1)).repeat([sz[0],1])
else:
    grad_moms[T-sz[0]-1:-1,:,1] = torch.linspace(-int(sz[1]/2),int(sz[1]/2-1),int(NRep)).repeat([sz[0],1])  
    
grad_moms[-1,:,:] = grad_moms[-2,:,:]

# dont optimize y  grads
#grad_moms[:,:,1] = 0


grad_moms = setdevice(grad_moms)

# event timing vector 
event_time = torch.from_numpy(1e-2*np.zeros((scanner.T,scanner.NRep,1))).float()
event_time[1,:,0] = 1e0  
event_time[-1,:,0] = 1e4
event_time = setdevice(event_time)

scanner.init_gradient_tensor_holder()
scanner.set_gradient_precession_tensor(grad_moms)


#############################################################################
## Forward process ::: ######################################################
    

# forward/adjoint pass
scanner.forward(spins, event_time)
#scanner.signal[:,:,0,:,:] = 0
scanner.adjoint(spins)

# try to fit this
target = scanner.reco.clone()
   
# save sequence parameters and target image to holder object
targetSeq = core.target_seq_holder.TargetSequenceHolder(flips,event_time,grad_moms,scanner,spins,target)


plt.close('all')
if True:                                                       # check sanity
    #imshow(spins.images[:,:,0], 'original')
    imshow(magimg(tonumpy(targetSeq.target).reshape([sz[0],sz[1],2])), 'reconstruction')
    
    stop()
    
# %% ###     OPTIMIZATION functions phi and init ######################################################
#############################################################################    
    
def phi_FRP_model(opt_params,aux_params):
    
    flips,grads,event_time,adc_mask = opt_params
    use_periodic_grad_moms_cap,_ = aux_params
    
    flip_mask = torch.zeros((scanner.T, scanner.NRep)).float()        
    flip_mask[:2,:] = 1
    flip_mask = setdevice(flip_mask)
    flips.register_hook(lambda x: flip_mask*x)
    
    scanner.init_flip_tensor_holder()
    scanner.set_flip_tensor(flips)
    
    # gradients
    grad_moms = torch.cumsum(grads,0)
    #grad_moms = grads * 1
    
    #grad_moms = grad_moms[:,1,:].unsqueeze(1).repeat([1,2,1])
    
    # dont optimize y  grads
    grad_moms[:,:,1] = 0
    
    if use_periodic_grad_moms_cap:
        fmax = torch.ones([1,1,2]).float()
        fmax = setdevice(fmax)
        fmax[0,0,0] = sz[0]/2
        fmax[0,0,1] = sz[1]/2
#
        grad_moms = torch.sin(grad_moms)*fmax

    scanner.init_gradient_tensor_holder()          
    scanner.set_gradient_precession_tensor(grad_moms)
    
    #scanner.adc_mask = adc_mask
          
    # forward/adjoint pass
    scanner.forward(spins, event_time)
    #scanner.signal[:,:,0,:,:] = 0
    scanner.adjoint(spins)

            
    loss = (scanner.reco - targetSeq.target)
    #phi = torch.sum((1.0/NVox)*torch.abs(loss.squeeze())**2)
    phi = torch.sum(loss.squeeze()**2/NVox)
    
    ereco = tonumpy(scanner.reco.detach()).reshape([sz[0],sz[1],2])
    error = e(tonumpy(targetSeq.target).ravel(),ereco.ravel())
    
    return (phi,scanner.reco, error)
    

def init_variables():
    
    np.random.seed(10)
    
    use_gtruth_grads = False
    if use_gtruth_grads:
        opt.use_periodic_grad_moms_cap = 0
        grad_moms = targetSeq.grad_moms.clone()
        
        padder = torch.zeros((1,scanner.NRep,2),dtype=torch.float32)
        padder = scanner.setdevice(padder)
        temp = torch.cat((padder,grad_moms),0)
        grads = temp[1:,:,:] - temp[:-1,:,:]   
    else:
        opt.use_periodic_grad_moms_cap = 1
        g = (np.random.rand(T,NRep,2) - 0.5)
        #g = (np.random.rand(T,NRep,2) - 0.5)*2*np.pi
        #g = (np.random.rand(T,NRep,2) - 0.5)*2*sz[0]/2
        #g = (np.random.rand(T,NRep,2) - 0.5)*0.1
        
        grads = torch.from_numpy(g).float()
        grads[:,:,1] = 0
        grads = setdevice(grads)
    
    grads.requires_grad = True
    
    flips = targetSeq.flips.clone()
    #flips[0,1] = 0
    flips.requires_grad = True
   
    event_time = targetSeq.event_time.clone()
    event_time.requires_grad = True

    adc_mask = targetSeq.adc_mask.clone()
    adc_mask.requires_grad = True     
    
    return [flips, grads, event_time, adc_mask]
    

    
# %% # OPTIMIZATION land

opt = core.opt_helper.OPT_helper(scanner,spins,None,1)
opt.set_target(tonumpy(targetSeq.target).reshape([sz[0],sz[1],2]))

opt.use_periodic_grad_moms_cap = 1           # do not sample above Nyquist flag
opt.learning_rate = 0.01                                        # ADAM step size
opt.optimzer_type = 'Adam'

# fast track
# opt.training_iter = 10; opt.training_iter_restarts = 5

print('<seq> now (with 10 iterations and several random initializations)')
opt.opti_mode = 'seq'

#target_numpy = tonumpy(target).reshape([sz[0],sz[1],2])
#imshow(magimg(target_numpy), 'target')

opt.set_opt_param_idx([1])
opt.custom_learning_rate = [0.1,0.005,0.1,0.1]
#opt.custom_learning_rate = [0.01,100.001,0.01,0.1]

opt.set_handles(init_variables, phi_FRP_model)
opt.scanner_opt_params = opt.init_variables()


opt.train_model_with_restarts(nmb_rnd_restart=10, training_iter=10,do_vis_image=True)
#opt.train_model_with_restarts(nmb_rnd_restart=1, training_iter=1)

#stop()

print('<seq> now (100 iterations with best initialization')
#opt.scanner_opt_params = opt.init_variables()
opt.train_model(training_iter=2000, do_vis_image=True)
#opt.train_model(training_iter=10)


#event_time = torch.abs(event_time)  # need to be positive
#opt.scanner_opt_params = init_variables()

_,reco,error = phi_FRP_model(opt.scanner_opt_params, opt.aux_params)
reco = tonumpy(reco).reshape([sz[0],sz[1],2])

#e(target_numpy.ravel(),reco.ravel())

target_numpy = tonumpy(targetSeq.target).reshape([sz[0],sz[1],2])
imshow(magimg(target_numpy), 'target')
imshow(magimg(reco), 'reconstruction')

stop()

# %% ###     PLOT RESULTS ######################################################@
#############################################################################

_,reco,error = phi_FRP_model(opt.scanner_opt_params, opt.aux_params)

opt.print_status(True, reco)

print("e: %f, total flipangle is %f °, total scan time is %f s," % (error, np.abs(tonumpy(opt.scanner_opt_params[0].permute([1,0]))).sum()*180/np.pi, tonumpy(torch.abs(opt.scanner_opt_params[2])[:,:,0].permute([1,0])).sum() ))




