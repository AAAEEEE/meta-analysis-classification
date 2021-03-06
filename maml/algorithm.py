from collections import defaultdict, OrderedDict
import torch

from maml.grad import soft_clip, get_grad_norm, get_grad_quantiles
from maml.utils import accuracy

from maml.models.lstm_embedding_model import LSTMAttentionEmbeddingModel

class Algorithm(object):
    def __init__(self):
        pass

    def inner_loop_adapt(self, task, num_updates=None, analysis=False, iter=None):
        raise NotImplementedError('Subclass should implement inner_loop')

    def predict_without_adapt(self, train_task, batch, param_dict=None):
        raise NotImplementedError()

class MAML_inner_algorithm(Algorithm):
    def __init__(self, model, inner_loss_func, fast_lr,
                first_order, num_updates, inner_loop_grad_clip,
                inner_loop_soft_clip_slope, device, is_classification=False):
        self._model = model
        self._inner_loss_func = inner_loss_func
        self._fast_lr = fast_lr # per step inner loop learning rate
        self._first_order = first_order
        self._num_updates = num_updates
        self._inner_loop_grad_clip = inner_loop_grad_clip
        self._inner_loop_soft_clip_slope = inner_loop_soft_clip_slope
        self._device = device
        self.is_classification = is_classification
        self.to(self._device)

    def inner_loop_one_step_gradient_descent(self, task, param_dict, return_grad_list=False):
        """Apply one step of gradient descent on self._inner_loss_func,
        based on data in the single task from argument task
        with respect to parameters in param_dict
        with step-size `self._fast_lr`, and returns
            the updated parameters
            loss before adaptation
            gradient if return_grad_list=True
        """
        preds = self._model(task.x, params=param_dict)
        loss = self._inner_loss_func(preds, task.y)

        measurements = {}
        measurements['loss'] = loss.item()
        if self.is_classification: measurements['accu'] = accuracy(preds, task.y)

        create_graph = not self._first_order
        grad_list = torch.autograd.grad(loss, param_dict.values(),
                                    create_graph=create_graph, allow_unused=False)
        # allow_unused If False, specifying inputs that were not used when computing outputs
        # (and therefore their grad is always zero) is an error. Defaults to False.

        clip_grad = (self._inner_loop_grad_clip > 0)
        if clip_grad:
            clip_grad_list = []
        for (name, param), grad in zip(param_dict.items(), grad_list):
            # grad will be torch.Tensor
            assert grad is not None
            if clip_grad:
                grad = soft_clip(grad,
                                 clip_value=self._inner_loop_grad_clip,
                                 slope=self._inner_loop_soft_clip_slope)
                clip_grad_list.append(grad)
            param_dict[name] = param - self._fast_lr * grad

        if return_grad_list:
            if clip_grad:
                grad_list = clip_grad_list
        else:
            grad_list = None
        return param_dict, measurements, grad_list

    def inner_loop_adapt(self, task, num_updates=None, analysis=False, iter=None):
        # adapt means doing the complete inner loop update
        measurements_trajectory = defaultdict(list)
        if analysis:
            grad_norm_by_step = [] # records the gradient norm at every inner loop step
            grad_quantiles_by_step = defaultdict(list)

        adapted_param_dict = self._model.param_dict
        if num_updates is None:
            # if num_updates is not specified
            # apply inner loop update for self._num_updates times
            num_updates = self._num_updates

        for i in range(num_updates):
            # here model is just a functional template
            # all of the parameters are passed in through params and embeddings 
            # measurements: dictionary mapping measurement_name to value
            adapted_param_dict, measurements, grad_list = \
                self.inner_loop_one_step_gradient_descent(task=task,
                                                          param_dict=adapted_param_dict,
                                                          return_grad_list=analysis)
            # add this step's measurement to its trajectory
            for key in measurements.keys():
                measurements_trajectory[key].append(measurements[key])

            if analysis:
                grad_norm_by_step.append(get_grad_norm(grad_list))
                grad_quantiles_by_step[i+1].extend(get_grad_quantiles(grad_list))
        
        with torch.no_grad(): # compute the train measurements after the last adaptation 
            preds = self._model(task.x, params=adapted_param_dict)
            loss = self._inner_loss_func(preds, task.y)
            measurements_trajectory['loss'].append(loss.item())
            if self.is_classification: measurements_trajectory['accu'].append(accuracy(preds, task.y))

        info_dict = None
        if analysis:
            info_dict = {}
            info_dict['grad_norm_by_step'] = grad_norm_by_step
            info_dict['grad_quantiles_by_step'] = grad_quantiles_by_step

        return adapted_param_dict, measurements_trajectory, info_dict

    def predict_without_adapt(self, train_task, batch, param_dict=None):
        # for MAML to make a prediction we don't need to see the train_task
        return self._model(batch=batch,
                           params=param_dict)
    
    def to(self, device, **kwargs):
        # called in __init__
        self._device = device
        self._model.to(device, **kwargs)
    
    def state_dict(self):
        # for model saving and reloading
        return {'model': self._model.state_dict()}


class MMAML_inner_algorithm(Algorithm):
    def __init__(self, model, embedding_model, inner_loss_func, fast_lr,
                first_order, num_updates, inner_loop_grad_clip,
                inner_loop_soft_clip_slope, device, is_classification=False):
        self._model = model
        self._embedding_model = embedding_model # produce the layerwise modulation
        self._inner_loss_func = inner_loss_func
        self._fast_lr = fast_lr # per step inner loop learning rate
        self._first_order = first_order
        self._num_updates = num_updates
        self._inner_loop_grad_clip = inner_loop_grad_clip
        self._inner_loop_soft_clip_slope = inner_loop_soft_clip_slope
        self._device = device
        self.is_classification = is_classification

        self.to(device)
    
    def inner_loop_one_step_gradient_descent(self, task, layer_modulations, param_dict, return_grad_list=False):
        """Apply one step of gradient descent on self._inner_loss_func,
        based on data in the single task from argument task
        with step-size `self._fast_lr`, and returns
            the updated parameters
            loss before adaptation
            gradient if return_grad_list=True
        """
        preds = self._model(task.x, params=param_dict, layer_modulations=layer_modulations)
        loss = self._inner_loss_func(preds, task.y)

        measurements = {}
        measurements['loss'] = loss.item()
        if self.is_classification: measurements['accu'] = accuracy(preds, task.y)

        create_graph = not self._first_order
        grad_list = torch.autograd.grad(loss, param_dict.values(),
                                        create_graph=create_graph, allow_unused=False)
        # allow_unused If False, specifying inputs that were not used when computing outputs
        # (and therefore their grad is always zero) is an error. Defaults to False.

        clip_grad = (self._inner_loop_grad_clip > 0)
        if clip_grad:
            clip_grad_list = []
        for (name, param), grad in zip(param_dict.items(), grad_list):
            # grad will be torch.Tensor
            assert grad is not None
            if clip_grad:
                grad = soft_clip(grad,
                                 clip_value=self._inner_loop_grad_clip,
                                 slope=self._inner_loop_soft_clip_slope)
                clip_grad_list.append(grad)

            param_dict[name] = param - self._fast_lr * grad

        if return_grad_list:
            if clip_grad:
                grad_list = clip_grad_list
        else:
            grad_list = None
        return param_dict, measurements, grad_list

    def inner_loop_adapt(self, task, num_updates=None, analysis=False, iter=None):
        # adapt means doing the complete inner loop update
        measurements_trajectory = defaultdict(list)
        if analysis:
            grad_norm_by_step = [] # records the gradient norm at every inner loop step
            grad_quantiles_by_step = defaultdict(list)

        adapted_param_dict = self._model.param_dict # parameters to be updated in the inner loop
        if isinstance(self._embedding_model, LSTMAttentionEmbeddingModel):
            layer_modulations = self._embedding_model(task, return_task_embedding=False, iter=iter)
        else: 
            layer_modulations = self._embedding_model(task, return_task_embedding=False)
        # apply inner loop update for self._num_updates times
        if num_updates is None:
            num_updates = self._num_updates

        for i in range(num_updates):
            # here model is just a functional template
            # all of the parameters are passed in through params and embeddings 
            adapted_param_dict, measurements, grad_list = \
                                self.inner_loop_one_step_gradient_descent(
                                    task=task,
                                    layer_modulations=layer_modulations,
                                    param_dict=adapted_param_dict,
                                    return_grad_list=analysis)
            # add this step's measurement to its trajectory
            for key in measurements.keys():
                measurements_trajectory[key].append(measurements[key])

            if analysis:
                grad_norm_by_step.append(get_grad_norm(grad_list))
                grad_quantiles_by_step[i+1].extend(get_grad_quantiles(grad_list))
        
        with torch.no_grad(): # compute the train loss after the last adaptation 
            preds = self._model(task.x, params=adapted_param_dict, layer_modulations=layer_modulations)
            loss = self._inner_loss_func(preds, task.y)
            measurements_trajectory['loss'].append(loss.item())
            if self.is_classification: measurements_trajectory['accu'].append(accuracy(preds, task.y))

        info_dict = None
        if analysis:
            info_dict = {}
            info_dict['grad_norm_by_step'] = grad_norm_by_step
            info_dict['grad_quantiles_by_step'] = grad_quantiles_by_step
            info_dict['layer_modulations'] = layer_modulations
        return adapted_param_dict, measurements_trajectory, info_dict

    
    def predict_without_adapt(self, train_task, batch, param_dict=None):
        layer_modulations = self._embedding_model(train_task, return_task_embedding=False)
        return self._model(batch=batch,
                           params=param_dict,
                           layer_modulations=layer_modulations)

    def to(self, device, **kwargs):
        # update _device field
        self._device = device
        self._model.to(device, **kwargs)
        self._embedding_model.to(device, **kwargs)
    
    def state_dict(self):
        # for model saving and reloading
        return {'model': self._model.state_dict(),
                'embedding_model': self._embedding_model.state_dict()}



class ModMAML_inner_algorithm(Algorithm):
    def __init__(self, model, layer_modulations, inner_loss_func, fast_lr,
                first_order, num_updates, inner_loop_grad_clip,
                inner_loop_soft_clip_slope, device, is_classification=False):
        self._model = model
        self._layer_modulations = layer_modulations
        self._inner_loss_func = inner_loss_func
        self._fast_lr = fast_lr # per step inner loop learning rate
        self._first_order = first_order
        self._num_updates = num_updates
        self._inner_loop_grad_clip = inner_loop_grad_clip
        self._inner_loop_soft_clip_slope = inner_loop_soft_clip_slope
        self._device = device
        self.is_classification = is_classification
        self.to(self._device)

    
    def inner_loop_one_step_gradient_descent(self, task, param_dict, return_grad_list=False, layer_modulations=None):
        """Apply one step of gradient descent on self._inner_loss_func,
        based on data in the single task from argument task
        with respect to parameters in param_dict
        with step-size `self._fast_lr`, and returns
            the updated parameters
            loss before adaptation
            gradient if return_grad_list=True
        """
        preds = self._model(task.x, params=param_dict, layer_modulations=layer_modulations)
        loss = self._inner_loss_func(preds, task.y)

        measurements = {}
        measurements['loss'] = loss.item()
        if self.is_classification: measurements['accu'] = accuracy(preds, task.y)

        create_graph = not self._first_order
        grad_list = torch.autograd.grad(loss, param_dict.values(),
                                    create_graph=create_graph, allow_unused=False)
        # allow_unused If False, specifying inputs that were not used when computing outputs
        # (and therefore their grad is always zero) is an error. Defaults to False.

        clip_grad = (self._inner_loop_grad_clip > 0)
        if clip_grad:
            clip_grad_list = []
        for (name, param), grad in zip(param_dict.items(), grad_list):
            # grad will be torch.Tensor
            assert grad is not None
            if clip_grad:
                grad = soft_clip(grad,
                                 clip_value=self._inner_loop_grad_clip,
                                 slope=self._inner_loop_soft_clip_slope)
                clip_grad_list.append(grad)
            param_dict[name] = param - self._fast_lr * grad

        if return_grad_list:
            if clip_grad:
                grad_list = clip_grad_list
        else:
            grad_list = None
        return param_dict, measurements, grad_list

    def inner_loop_adapt(self, task, num_updates=None, analysis=False, iter=None):
        # adapt means doing the complete inner loop update
        measurements_trajectory = defaultdict(list)
        if analysis:
            grad_norm_by_step = [] # records the gradient norm at every inner loop step
            grad_quantiles_by_step = defaultdict(list)
        
        adapted_param_dict = self._model.param_dict
        if num_updates is None:
            # if num_updates is not specified
            # apply inner loop update for self._num_updates times
            num_updates = self._num_updates

        for i in range(num_updates):
            # here model is just a functional template
            # all of the parameters are passed in through params and embeddings 
            adapted_param_dict, measurements, grad_list = \
                self.inner_loop_one_step_gradient_descent(task=task,
                                                          param_dict=adapted_param_dict,
                                                          return_grad_list=analysis,
                                                          layer_modulations=self._layer_modulations)
            # add this step's measurement to its trajectory
            for key in measurements.keys():
                measurements_trajectory[key].append(measurements[key])

            if analysis:
                grad_norm_by_step.append(get_grad_norm(grad_list))
                grad_quantiles_by_step[i+1].extend(get_grad_quantiles(grad_list))
        
        with torch.no_grad(): # compute the train loss after the last adaptation 
            preds = self._model(task.x, params=adapted_param_dict)
            loss = self._inner_loss_func(preds, task.y)
            measurements_trajectory['loss'].append(loss.item())
            if self.is_classification: measurements_trajectory['accu'].append(accuracy(preds, task.y))

        info_dict = None
        if analysis:
            info_dict = {}
            info_dict['grad_norm_by_step'] = grad_norm_by_step
            info_dict['grad_quantiles_by_step'] = grad_quantiles_by_step
        
        return adapted_param_dict, measurements_trajectory, info_dict

    def predict_without_adapt(self, train_task, batch, param_dict=None):
        # for MAML to make a prediction we don't need to see the train_task
        return self._model(batch=batch,
                           params=param_dict)
    
    def to(self, device, **kwargs):
        # called in __init__
        self._device = device
        self._model.to(device, **kwargs)
        self._model.to(device, **kwargs)
        self._layer_modulations.to(device, **kwargs)
    
    def state_dict(self):
        # for model saving and reloading
        return {'model': self._model.state_dict(),
                'layer_modulations': self._layer_modulations}


class RegMAML_inner_algorithm(Algorithm):
    def __init__(self, model, embedding_model, inner_loss_func, fast_lr,
                first_order, num_updates, inner_loop_grad_clip,
                inner_loop_soft_clip_slope, device, is_classification=False):
        self._model = model
        self._embedding_model = embedding_model
        self._inner_loss_func = inner_loss_func
        self._fast_lr = fast_lr # per step inner loop learning rate
        self._first_order = first_order
        self._num_updates = num_updates
        self._inner_loop_grad_clip = inner_loop_grad_clip
        self._inner_loop_soft_clip_slope = inner_loop_soft_clip_slope
        self._device = device
        self.is_classification = is_classification
        self.to(self._device)

    
    def inner_loop_one_step_gradient_descent(self, task, adapted_param_dict, modulation, return_grad_list=False):
        """Apply one step of gradient descent on self._inner_loss_func,
        based on data in the single task from argument task
        with respect to parameters in param_dict
        with step-size `self._fast_lr`, and returns
            the updated parameters
            loss before adaptation
            gradient if return_grad_list=True
        """
        preds = self._model(task.x, modulation=modulation, update_params=adapted_param_dict)
        loss = self._inner_loss_func(preds, task.y)

        measurements = {}
        measurements['loss'] = loss.item()
        if self.is_classification: measurements['accu'] = accuracy(preds, task.y)

        create_graph = not self._first_order
        grad_list = torch.autograd.grad(loss, adapted_param_dict.values(),
                                create_graph=create_graph, allow_unused=False)
        # allow_unused If False, specifying inputs that were not used when computing outputs
        # (and therefore their grad is always zero) is an error. Defaults to False.

        clip_grad = (self._inner_loop_grad_clip > 0)
        if clip_grad:
            clip_grad_list = []
        for (name, param), grad in zip(adapted_param_dict.items(), grad_list):
            # grad will be torch.Tensor
            assert grad is not None
            if clip_grad:
                grad = soft_clip(grad,
                                 clip_value=self._inner_loop_grad_clip,
                                 slope=self._inner_loop_soft_clip_slope)
                clip_grad_list.append(grad)
            adapted_param_dict[name] = param - self._fast_lr * grad

        if return_grad_list:
            if clip_grad:
                grad_list = clip_grad_list
        else:
            grad_list = None
        return adapted_param_dict, measurements, grad_list

    def inner_loop_adapt(self, task, num_updates=None, analysis=False, iter=None):
        # adapt means doing the complete inner loop update
        measurements_trajectory = defaultdict(list)
        if analysis:
            grad_norm_by_step = [] # records the gradient norm at every inner loop step
            grad_quantiles_by_step = defaultdict(list)
        
        adapted_param_dict = OrderedDict()
        adapted_param_dict['classifier.fully_connected.weight'] = self._model.classifier.fully_connected.weight
        adapted_param_dict['classifier.fully_connected.bias'] = self._model.classifier.fully_connected.bias
        
        modulation = self._embedding_model(task, return_task_embedding=False)

        if num_updates is None:
            # if num_updates is not specified
            # apply inner loop update for self._num_updates times
            num_updates = self._num_updates

        for i in range(num_updates):
            # here model is just a functional template
            # all of the parameters are passed in through params and embeddings 
            adapted_param_dict, measurements, grad_list = \
                self.inner_loop_one_step_gradient_descent(task=task,
                                                          adapted_param_dict=adapted_param_dict,
                                                          modulation=modulation,
                                                          return_grad_list=analysis)
            # add this step's measurement to its trajectory
            for key in measurements.keys():
                measurements_trajectory[key].append(measurements[key])

            if analysis:
                grad_norm_by_step.append(get_grad_norm(grad_list))
                grad_quantiles_by_step[i+1].extend(get_grad_quantiles(grad_list))
        
        with torch.no_grad(): # compute the train loss after the last adaptation 
            preds = self._model(task.x, modulation=modulation, update_params=adapted_param_dict)
            loss = self._inner_loss_func(preds, task.y)
            measurements_trajectory['loss'].append(loss.item())
            if self.is_classification: measurements_trajectory['accu'].append(accuracy(preds, task.y))

        info_dict = None
        if analysis:
            info_dict = {}
            info_dict['grad_norm_by_step'] = grad_norm_by_step
            info_dict['grad_quantiles_by_step'] = grad_quantiles_by_step
            info_dict['modulation'] = modulation
        return adapted_param_dict, measurements_trajectory, info_dict

    def predict_without_adapt(self, train_task, batch, param_dict=None):
        # param_dict only contains the changed parameters in the last linear layer
        modulation = self._embedding_model(train_task, return_task_embedding=False)
        return self._model(batch=batch,
                           update_params=param_dict,
                           modulation=modulation)
    
    def to(self, device, **kwargs):
        # called in __init__
        self._device = device
        self._model.to(device, **kwargs)
        self._embedding_model.to(device, **kwargs)
    
    def state_dict(self):
        # for model saving and reloading
        return {'model': self._model.state_dict(),
                'embedding_model': self._embedding_model.state_dict()}