# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# Copyright (c) 2018, NVIDIA CORPORATION. All rights reserved.
import datetime
import logging
import time

import torch
import torch.distributed as dist

from maskrcnn_benchmark.utils.comm import get_world_size
from maskrcnn_benchmark.utils.metric_logger import MetricLogger
import statistics
try:
    from apex import amp
    use_amp = True
except ImportError:
    print('Use APEX for multi-precision via apex.amp')
    use_amp = False

def reduce_loss_dict(loss_dict):
    """
    Reduce the loss dictionary from all processes so that process with rank
    0 has the averaged results. Returns a dict with the same fields as
    loss_dict, after reduction.
    """
    world_size = get_world_size()
    if world_size < 2:
        return loss_dict
    with torch.no_grad():
        loss_names = []
        all_losses = []
        for k in sorted(loss_dict.keys()):
            loss_names.append(k)
            all_losses.append(loss_dict[k])
        all_losses = torch.stack(all_losses, dim=0)
        dist.reduce(all_losses, dst=0)
        if dist.get_rank() == 0:
            # only main process gets accumulated, so only divide by
            # world_size in this case
            all_losses /= world_size
        reduced_losses = {k: v for k, v in zip(loss_names, all_losses)}
    return reduced_losses


def do_train(
    model,
    data_loader,
    optimizer,
    scheduler,
    checkpointer,
    device,
    checkpoint_period,
    arguments,
    use_amp,
    cfg,
    dllogger,
    per_iter_end_callback_fn=None,
):
    dllogger.log(step="PARAMETER", data={"train_start": True})
    logger = logging.getLogger("maskrcnn_benchmark.trainer")
    logger.info("Start training")
    meters = MetricLogger(delimiter="  ")
    max_iter = len(data_loader)
    print("max_iter: ", max_iter)
    start_iter = arguments["iteration"]
    model.train()
    start_training_time = time.time()
    end = time.time()
    TIME_DATA_TO_GPU = [0.0]*max_iter
    TIME_FORWARD = [0.0]*max_iter
    TIME_BACKWARD = [0.0]*max_iter
    TIME_REDUCE = [0.0]*max_iter
    TIME_OPTIMIZER = [0.0]*max_iter
    TIME_LOGGING = [0.0]*max_iter
    TIME_DATALOADER = [0.0]*max_iter

    for iteration, (images, targets, _) in enumerate(data_loader, start_iter):
        data_time = time.time() - end
        TIME_DATALOADER[iteration] = data_time 

        iteration = iteration + 1
        print(" ###### iteration: " , iteration)
        arguments["iteration"] = iteration
        # TIME: DATA TO GPU
        time_data_start = time.time()
        images = images.to(device)
        targets = [target.to(device) for target in targets]
        if iteration < len(TIME_DATA_TO_GPU):
            TIME_DATA_TO_GPU[iteration-1] = time.time() - time_data_start

 
        # TIME: FORWARD PASS
        time_forward_start = time.time()

        loss_dict = model(images, targets)

        losses = sum(loss for loss in loss_dict.values())
        if iteration < len(TIME_DATA_TO_GPU):
            TIME_FORWARD[iteration-1] = time.time() - time_forward_start

        # TIME: ALL REDUCE
        # reduce losses over all GPUs for logging purposes
        time_reduce_start = time.time()

        loss_dict_reduced = reduce_loss_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        meters.update(loss=losses_reduced, **loss_dict_reduced)
        if iteration < len(TIME_DATA_TO_GPU):
            TIME_REDUCE[iteration-1] = time.time() - time_reduce_start


        # TIME: BACKWARD
        # Note: If mixed precision is not used, this ends up doing nothing
        # Otherwise apply loss scaling for mixed-precision recipe
        time_backward_start = time.time()

        if use_amp:
            with amp.scale_loss(losses, optimizer) as scaled_losses:
                scaled_losses.backward()
        else:
            losses.backward()
        if iteration < len(TIME_DATA_TO_GPU):    
            TIME_BACKWARD[iteration-1] = time.time() - time_backward_start

        # TIME: OPTIMZER
        time_optim_start = time.time()

        if not cfg.SOLVER.ACCUMULATE_GRAD:
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
        else:
            if (iteration + 1) % cfg.SOLVER.ACCUMULATE_STEPS == 0:
                for param in model.parameters():
                    if param.grad is not None:
                        param.grad.data.div_(cfg.SOLVER.ACCUMULATE_STEPS)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()
        if iteration < len(TIME_DATA_TO_GPU):
            TIME_OPTIMIZER[iteration-1] = time.time() - time_optim_start


        # TIME: LOGGING
        time_logging_start = time.time()

        batch_time = time.time() - end
        end = time.time()
        meters.update(time=batch_time, data=data_time)

        eta_seconds = meters.time.global_avg * (max_iter - iteration)
        eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

        if iteration % 20 == 0:
            logger.info("iter: %d batch_time: %f" % (iteration, batch_time))

        if (iteration > 500):
            meters.update(time=batch_time, data=data_time)

            eta_seconds = meters.time.global_avg * (max_iter - iteration)
            eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

            if iteration % 20 == 0 or iteration == max_iter:
                logger.info(
                    meters.delimiter.join(
                        [
                            "eta: {eta}",
                            "avg iteration time(s): {avg_iter:.2f}",
                            "avg iter/s: {iter_s:.2f}",
                            "iter: {iter}",
                            "{meters}",
                            "lr: {lr:.6f}",
                            "max mem: {memory:.0f}",
                        ]
                    ).format(
                        eta=eta_string,
                        avg_iter=meters.time.global_avg,
                        iter_s=1.0 / meters.time.global_avg,
                        iter=iteration,
                        meters=str(meters),
                        lr=optimizer.param_groups[0]["lr"],
                        memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                    )
                )

        if iteration % checkpoint_period == 0:
            checkpointer.save("model_{:07d}".format(iteration), **arguments)
        if iteration == max_iter:
            checkpointer.save("model_final", **arguments)

        if iteration < len(TIME_DATA_TO_GPU):
            TIME_LOGGING[iteration-1] = time.time() - time_logging_start


        # per-epoch work (testing)
        if per_iter_end_callback_fn is not None:
            early_exit = per_iter_end_callback_fn(iteration=iteration)
            if early_exit:
                break


    total_training_time = time.time() - start_training_time
    total_time_str = str(datetime.timedelta(seconds=total_training_time))
    dllogger.log(step=tuple(), data={"e2e_train_time": total_training_time,
                                        "train_perf_fps": max_iter * cfg.SOLVER.IMS_PER_BATCH / total_training_time})

    print(      "mean TIME_DATA_TO_GPU ",   statistics.mean(TIME_DATA_TO_GPU),
                                        "mean TIME_FORWARD ", statistics.mean(TIME_FORWARD),
                                        "mean TIME_BACKWARD" , statistics.mean(TIME_BACKWARD),
                                        "mean TIME_REDUCE " ,  statistics.mean(TIME_REDUCE),
                                        "mean TIME_OPTIMIZER " , statistics.mean(TIME_OPTIMIZER),
                                        "mean TIME_LOGGING" , statistics.mean(TIME_LOGGING),
                                        "mean TIME_DATALOADER" , statistics.mean(TIME_DATALOADER),
                                        "std TIME_DATA_TO_GPU" ,   statistics.stdev(TIME_DATA_TO_GPU),
                                        "std TIME_FORWARD" , statistics.stdev(TIME_FORWARD),
                                        "std TIME_BACKWARD" , statistics.stdev(TIME_BACKWARD),
                                        "std TIME_REDUCE" , statistics.stdev(TIME_REDUCE),
                                        "std TIME_OPTIMIZER" , statistics.stdev(TIME_OPTIMIZER),
                                        "std TIME_LOGGING" , statistics.stdev(TIME_LOGGING),
                                        "std TIME_DATALOADER" , statistics.stdev(TIME_DATALOADER))

    logger = logging.getLogger("maskrcnn_benchmark.trainer")
    sec_per_iteration = total_training_time / max_iter
    samples_per_sec = int(cfg.SOLVER.IMS_PER_BATCH) / sec_per_iteration
    logger.info(
        "Total training time: {} ({:.4f} s / it) throughput: {:.2f} FPS".format(
            total_time_str, sec_per_iteration, samples_per_sec
        )
    )
    logger.info(" ########   mean TIME_DATA_TO_GPU  : {}, mean TIME_FORWARD : {}, \
                                        mean TIME_BACKWARD : {} \
                                        mean TIME_REDUCE  : {} \
                                        mean TIME_OPTIMIZER : {} \
                                        mean TIME_LOGGING : {} \
                                        mean TIME_DATALOADER : {} \
                                        std TIME_DATA_TO_GPU : {} \
                                        std TIME_FORWARD : {} \
                                        std TIME_BACKWARD : {} \
                                        std TIME_REDUCE : {} \
                                        std TIME_OPTIMIZER : {} \
                                        std TIME_LOGGING ,: {} \
                                        std TIME_DATALOADER ".format(statistics.mean(TIME_DATA_TO_GPU), 
                                        statistics.mean(TIME_FORWARD),
                                        statistics.mean(TIME_BACKWARD),
                                         statistics.mean(TIME_REDUCE),
                                         statistics.mean(TIME_OPTIMIZER),
                                         statistics.mean(TIME_LOGGING),
                                         statistics.mean(TIME_DATALOADER),
                                         statistics.stdev(TIME_DATA_TO_GPU),
                                         statistics.stdev(TIME_FORWARD),
                                         statistics.stdev(TIME_BACKWARD),
                                         statistics.stdev(TIME_REDUCE),
                                         statistics.stdev(TIME_OPTIMIZER),
                                         statistics.stdev(TIME_LOGGING),
                                         statistics.stdev(TIME_DATALOADER)))

    logger.info("Final Loss at iteration {}: {}".format(max_iter, str(meters)))

