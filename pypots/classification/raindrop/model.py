"""
The implementation of Raindrop for the partially-observed time-series classification task.

"""

# Created by Wenjie Du <wenjay.du@gmail.com>
# License: BSD-3-Clause


from typing import Union, Optional

import numpy as np
import torch
from torch.utils.data import DataLoader

from .core import _Raindrop
from ..grud.data import DatasetForGRUD
from ...classification.base import BaseNNClassifier
from ...nn.functional import gather_listed_dicts
from ...nn.modules.loss import Criterion, CrossEntropy
from ...optim.adam import Adam
from ...optim.base import Optimizer


class Raindrop(BaseNNClassifier):
    """The PyTorch implementation of the Raindrop model :cite:`zhang2022Raindrop`.

    Parameters
    ----------
    n_steps :
        The number of time steps in the time-series data sample.

    n_features :
        The number of features in the time-series data samples.

    n_classes :
        The number of classes in the classification task.

    n_layers :
        The number of layers in the Transformer encoder in the Raindrop model.

    d_model :
        The dimension of the Transformer encoder backbone.
        It is the input dimension of the multi-head self-attention layers.

    n_heads :
        The number of heads in the multi-head self-attention mechanism.

    d_ffn :
        The dimension of the layer in the Feed-Forward Networks (FFN).

    dropout :
        The dropout rate for all fully-connected layers in the model.

    d_static :
        The dimension of the static features.

    aggregation :
        The aggregation method for the Transformer encoder output.

    sensor_wise_mask :
        Whether to apply the sensor-wise masking.

    static :
        Whether to use the static features.

    batch_size :
        The batch size for training and evaluating the model.

    epochs :
        The number of epochs for training the model.

    patience :
        The patience for the early-stopping mechanism. Given a positive integer, the training process will be
        stopped when the model does not perform better after that number of epochs.
        Leaving it default as None will disable the early-stopping.

    training_loss:
        The customized loss function designed by users for training the model.
        If not given, will use the default loss as claimed in the original paper.

    validation_metric:
        The customized metric function designed by users for validating the model.
        If not given, will use the default loss from the original paper as the metric.

    optimizer :
        The optimizer for model training.
        If not given, will use a default Adam optimizer.

    num_workers :
        The number of subprocesses to use for data loading.
        `0` means data loading will be in the main process, i.e. there won't be subprocesses.

    device :
        The device for the model to run on. It can be a string, a :class:`torch.device` object, or a list of them.
        If not given, will try to use CUDA devices first (will use the default CUDA device if there are multiple),
        then CPUs, considering CUDA and CPU are so far the main devices for people to train ML models.
        If given a list of devices, e.g. ['cuda:0', 'cuda:1'], or [torch.device('cuda:0'), torch.device('cuda:1')] , the
        model will be parallely trained on the multiple devices (so far only support parallel training on CUDA devices).
        Other devices like Google TPU and Apple Silicon accelerator MPS may be added in the future.

    saving_path :
        The path for automatically saving model checkpoints and tensorboard files (i.e. loss values recorded during
        training into a tensorboard file). Will not save if not given.

    model_saving_strategy :
        The strategy to save model checkpoints. It has to be one of [None, "best", "better", "all"].
        No model will be saved when it is set as None.
        The "best" strategy will only automatically save the best model after the training finished.
        The "better" strategy will automatically save the model during training whenever the model performs
        better than in previous epochs.
        The "all" strategy will save every model after each epoch training.

    verbose :
        Whether to print out the training logs during the training process.
    """

    def __init__(
        self,
        n_steps,
        n_features,
        n_classes,
        n_layers,
        d_model,
        n_heads,
        d_ffn,
        dropout,
        d_static=0,
        aggregation="mean",
        sensor_wise_mask=False,
        static=False,
        batch_size=32,
        epochs=100,
        patience: Optional[int] = None,
        training_loss: Union[Criterion, type] = CrossEntropy,
        validation_metric: Union[Criterion, type] = CrossEntropy,
        optimizer: Union[Optimizer, type] = Adam,
        num_workers: int = 0,
        device: Optional[Union[str, torch.device, list]] = None,
        saving_path: str = None,
        model_saving_strategy: Optional[str] = "best",
        verbose: bool = True,
    ):
        super().__init__(
            n_classes=n_classes,
            training_loss=training_loss,
            validation_metric=validation_metric,
            batch_size=batch_size,
            epochs=epochs,
            patience=patience,
            num_workers=num_workers,
            device=device,
            saving_path=saving_path,
            model_saving_strategy=model_saving_strategy,
            verbose=verbose,
        )

        self.n_features = n_features
        self.n_steps = n_steps

        # set up the model
        self.model = _Raindrop(
            n_features=n_features,
            n_layers=n_layers,
            d_model=d_model,
            n_heads=n_heads,
            d_ffn=d_ffn,
            n_classes=n_classes,
            dropout=dropout,
            max_len=n_steps,
            d_static=d_static,
            aggregation=aggregation,
            sensor_wise_mask=sensor_wise_mask,
            static=static,
            training_loss=self.training_loss,
            validation_metric=self.validation_metric,
        )
        self._send_model_to_given_device()
        self._print_model_size()

        # set up the optimizer
        if isinstance(optimizer, Optimizer):
            self.optimizer = optimizer
        else:
            self.optimizer = optimizer()  # instantiate the optimizer if it is a class
            assert isinstance(self.optimizer, Optimizer)
        self.optimizer.init_optimizer(self.model.parameters())

    def _assemble_input_for_training(self, data: list) -> dict:
        # fetch data
        (
            indices,
            X,
            X_filledLOCF,
            missing_mask,
            deltas,
            empirical_mean,
            y,
        ) = self._send_data_to_given_device(data)

        bz, n_steps, n_features = X.shape
        lengths = torch.tensor([n_steps] * bz, dtype=torch.float)
        times = torch.tensor(range(n_steps), dtype=torch.float).repeat(bz, 1)

        inputs = {
            "X": X,
            "static": None,
            "timestamps": times,
            "lengths": lengths,
            "missing_mask": missing_mask,
            "y": y,
        }
        return inputs

    def _assemble_input_for_validating(self, data: list) -> dict:
        return self._assemble_input_for_training(data)

    def _assemble_input_for_testing(self, data: list) -> dict:
        (
            indices,
            X,
            X_filledLOCF,
            missing_mask,
            deltas,
            empirical_mean,
        ) = self._send_data_to_given_device(data)
        bz, n_steps, n_features = X.shape
        lengths = torch.tensor([n_steps] * bz, dtype=torch.float)
        times = torch.tensor(range(n_steps), dtype=torch.float).repeat(bz, 1)

        inputs = {
            "X": X,
            "static": None,
            "timestamps": times,
            "lengths": lengths,
            "missing_mask": missing_mask,
        }

        return inputs

    def fit(
        self,
        train_set: Union[dict, str],
        val_set: Optional[Union[dict, str]] = None,
        file_type: str = "hdf5",
    ) -> None:
        # Step 1: wrap the input data with classes Dataset and DataLoader
        train_dataset = DatasetForGRUD(train_set, file_type=file_type)
        train_dataloader = DataLoader(
            train_dataset,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
        )
        val_dataloader = None
        if val_set is not None:
            val_dataset = DatasetForGRUD(val_set, file_type=file_type)
            val_dataloader = DataLoader(
                val_dataset,
                batch_size=self.batch_size,
                shuffle=False,
                num_workers=self.num_workers,
            )

        # Step 2: train the model and freeze it
        self._train_model(train_dataloader, val_dataloader)
        self.model.load_state_dict(self.best_model_dict)

        # Step 3: save the model if necessary
        self._auto_save_model_if_necessary(confirm_saving=self.model_saving_strategy == "best")

    @torch.no_grad()
    def predict(
        self,
        test_set: Union[dict, str],
        file_type: str = "hdf5",
    ) -> dict:
        self.model.eval()  # set the model to evaluation mode

        # Step 1: wrap the input data with classes Dataset and DataLoader
        test_dataset = DatasetForGRUD(
            test_set,
            return_y=False,
            file_type=file_type,
        )
        test_dataloader = DataLoader(
            test_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
        )

        # Step 2: process the data with the model
        dict_result_collector = []
        for idx, data in enumerate(test_dataloader):
            inputs = self._assemble_input_for_testing(data)
            results = self.model(inputs)
            dict_result_collector.append(results)

        # Step 3: output collection and return
        result_dict = gather_listed_dicts(dict_result_collector)
        classification = np.argmax(result_dict["classification_proba"], axis=1)
        result_dict["classification"] = classification

        return result_dict
