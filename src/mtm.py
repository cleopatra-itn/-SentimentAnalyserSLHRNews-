import transformers
import torch
import torch.nn as nn
from torch.utils.data.sampler import RandomSampler
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.dataloader import DataLoader
from transformers.data.data_collator import DataCollator
from transformers.data.data_collator import DataCollatorWithPadding, InputDataClass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union
from transformers import is_torch_tpu_available
import numpy as np

class MultitaskModel(transformers.PreTrainedModel):
    def __init__(self, encoder, taskmodels_dict):
        """
        Setting MultitaskModel up as a PretrainedModel allows us
        to take better advantage of Trainer features
        """
        super().__init__(transformers.PretrainedConfig())

        self.encoder = encoder
        self.taskmodels_dict = nn.ModuleDict(taskmodels_dict)

    @classmethod
    def create(cls, model_name, model_type_dict, model_config_dict):
        """
        This creates a MultitaskModel using the model class and config objects
        from single-task models. 

        We do this by creating each single-task model, and having them share
        the same encoder transformer.
        """
        shared_encoder = None
        taskmodels_dict = {}
        do = nn.Dropout(p=0.2)
        for task_name, model_type in model_type_dict.items():
            model = model_type.from_pretrained(
                model_name,
                config=model_config_dict[task_name],
            )
            if shared_encoder is None:
                shared_encoder = getattr(
                    model, cls.get_encoder_attr_name(model))
            else:
                setattr(model, cls.get_encoder_attr_name(
                    model), shared_encoder)
            taskmodels_dict[task_name] = model
        return cls(encoder=shared_encoder, taskmodels_dict=taskmodels_dict)

    @classmethod
    def get_encoder_attr_name(cls, model):
        """
        The encoder transformer is named differently in each model "architecture".
        This method lets us get the name of the encoder attribute
        """
        model_class_name = model.__class__.__name__
        if model_class_name.startswith("Bert"):
            return "bert"
        elif model_class_name.startswith("Roberta"):
            return "roberta"
        elif model_class_name.startswith("Albert"):
            return "albert"
        else:
            raise KeyError(f"Add support for new model {model_class_name}")

    def forward(self, task_name, **kwargs):
        return self.taskmodels_dict[task_name](**kwargs)


class NLPDataCollator(DataCollatorWithPadding):  # DataCollatorWithPadding
    """
    Extending the existing DataCollator to work with NLP dataset batches
    """

    def collate_batch(self, features: List[Union[InputDataClass, Dict]]) -> Dict[str, torch.Tensor]:
        first = features[0]
        batch = None
        if isinstance(first, dict):
            # NLP data sets current works presents features as lists of dictionary
            # (one per example), so we  will adapt the collate_batch logic for that
            if "labels" in first and first["labels"] is not None:
                if first["labels"].dtype == torch.int64:
                    labels = torch.tensor([f["labels"]
                                           for f in features], dtype=torch.long)
                else:
                    labels = torch.tensor([f["labels"]
                                           for f in features], dtype=torch.float)
                batch = {"labels": labels}
            for k, v in first.items():
                if k != "labels" and v is not None and not isinstance(v, str):
                    batch[k] = torch.stack([f[k] for f in features])
            return batch
        else:
            # otherwise, revert to using the default collate_batch
            return DataCollatorWithPadding().collate_batch(features)


class StrIgnoreDevice(str):
    """
    This is a hack. The Trainer is going call .to(device) on every input
    value, but we need to pass in an additional `task_name` string.
    This prevents it from throwing an error
    """

    def to(self, device):
        return self


class DataLoaderWithTaskname:
    """
    Wrapper around a DataLoader to also yield a task name
    """

    def __init__(self, task_name, data_loader):
        self.task_name = task_name
        self.data_loader = data_loader

        self.batch_size = data_loader.batch_size
        self.dataset = data_loader.dataset

    def __len__(self):
        return len(self.data_loader)

    def __iter__(self):
        for batch in self.data_loader:
            batch["task_name"] = StrIgnoreDevice(self.task_name)
            yield batch


class MultitaskDataloader:
    """
    Data loader that combines and samples from multiple single-task
    data loaders.
    """

    def __init__(self, dataloader_dict):
        self.dataloader_dict = dataloader_dict
        self.num_batches_dict = {
            task_name: len(dataloader)
            for task_name, dataloader in self.dataloader_dict.items()
        }
        self.task_name_list = list(self.dataloader_dict)
        self.dataset = [None] * sum(
            len(dataloader.dataset)
            for dataloader in self.dataloader_dict.values()
        )

    def __len__(self):
        return sum(self.num_batches_dict.values())

    def __iter__(self):
        """
        For each batch, sample a task, and yield a batch from the respective
        task Dataloader.

        We use size-proportional sampling, but you could easily modify this
        to sample from some-other distribution.
        """
        task_choice_list = []
        for i, task_name in enumerate(self.task_name_list):
            task_choice_list += [i] * self.num_batches_dict[task_name]
        task_choice_list = np.array(task_choice_list)
        np.random.shuffle(task_choice_list)
        dataloader_iter_dict = {
            task_name: iter(dataloader)
            for task_name, dataloader in self.dataloader_dict.items()
        }
        for task_choice in task_choice_list:
            task_name = self.task_name_list[task_choice]
            yield next(dataloader_iter_dict[task_name])


class MultitaskTrainer(transformers.Trainer):

    def get_single_train_dataloader(self, task_name, train_dataset):
        """
        Create a single-task data loader that also yields task names
        """
        if self.train_dataset is None:
            raise ValueError("Trainer: training requires a train_dataset.")
        if False and is_torch_tpu_available():
            train_sampler = get_tpu_sampler(train_dataset)
        else:
            train_sampler = (
                RandomSampler(train_dataset)
                if self.args.local_rank == -1
                else DistributedSampler(train_dataset)
            )

        data_loader = DataLoaderWithTaskname(
            task_name=task_name,
            data_loader=DataLoader(
                train_dataset,
                batch_size=self.args.train_batch_size,
                sampler=train_sampler,
                collate_fn=self.data_collator.collate_batch,
            ),
        )
        return data_loader

    def get_train_dataloader(self):
        """
        Returns a MultitaskDataloader, which is not actually a Dataloader
        but an iterable that returns a generator that samples from each 
        task Dataloader
        """
        return MultitaskDataloader({
            task_name: self.get_single_train_dataloader(
                task_name, task_dataset)
            for task_name, task_dataset in self.train_dataset.items()
        })
    # New content
    # def get_single_eval_dataloader(self, task_name, eval_dataset):
    #     """
    #     Create a single-task data loader that also yields task names
    #     """
    #     if self.eval_dataset is None:
    #         raise ValueError("Trainer: evaluating requires a eval_dataset.")
    #     if False and is_tpu_available():
    #         eval_sampler = get_tpu_sampler(eval_dataset)
    #     else:
    #         eval_sampler = (
    #             RandomSampler(eval_dataset)
    #             if self.args.local_rank == -1
    #             else DistributedSampler(eval_dataset)
    #         )

    #     data_loader = DataLoaderWithTaskname(
    #         task_name=task_name,
    #         data_loader=DataLoader(
    #           eval_dataset,
    #           batch_size=self.args.eval_batch_size,
    #           sampler=eval_sampler,
    #           collate_fn=self.data_collator.collate_batch,
    #         ),
    #     )
    #     return data_loader

    # def get_eval_dataloader(self, dataset):
    #     """
    #     Returns a MultitaskDataloader, which is not actually a Dataloader
    #     but an iterable that returns a generator that samples from each
    #     task Dataloader
    #     """
    #     return MultitaskDataloader({
    #         task_name: self.get_single_eval_dataloader(task_name, task_dataset)
    #         for task_name, task_dataset in self.eval_dataset.items()
    #     })
    # def evaluate(
    #     self,
    #     eval_dataset: Optional[Dataset] = None,
    #     ignore_keys: Optional[List[str]] = None,
    #     metric_key_prefix: str = "eval",
    # ) -> Dict[str, float]:
    #     """
    #     Run evaluation and returns metrics.

    #     The calling script will be responsible for providing a method to compute metrics, as they are task-dependent
    #     (pass it to the init :obj:`compute_metrics` argument).

    #     You can also subclass and override this method to inject custom behavior.

    #     Args:
    #         eval_dataset (:obj:`Dataset`, `optional`):
    #             Pass a dataset if you wish to override :obj:`self.eval_dataset`. If it is an :obj:`datasets.Dataset`,
    #             columns not accepted by the ``model.forward()`` method are automatically removed. It must implement the
    #             :obj:`__len__` method.
    #         ignore_keys (:obj:`Lst[str]`, `optional`):
    #             A list of keys in the output of your model (if it is a dictionary) that should be ignored when
    #             gathering predictions.
    #         metric_key_prefix (:obj:`str`, `optional`, defaults to :obj:`"eval"`):
    #             An optional prefix to be used as the metrics key prefix. For example the metrics "bleu" will be named
    #             "eval_bleu" if the prefix is "eval" (default)

    #     Returns:
    #         A dictionary containing the evaluation loss and the potential metrics computed from the predictions. The
    #         dictionary also contains the epoch number which comes from the training state.
    #     """
    #     # if eval_dataset is not None and not isinstance(eval_dataset, collections.abc.Sized):
    #     #     raise ValueError("eval_dataset must implement __len__")

    #     eval_dataloader = self.get_eval_dataloader(eval_dataset)
    #     # start_time = time.time()
    #     for key, value in eval_dataloader.dataloader_dict.items():
    #         print(super().evaluate(value))
        # output = self.prediction_loop(
        #     eval_dataloader,
        #     description="Evaluation",
        #     # No point gathering the predictions if there are no metrics, otherwise we defer to
        #     # self.args.prediction_loss_only
        #     prediction_loss_only=True if self.compute_metrics is None else None,
        #     ignore_keys=ignore_keys,
        #     metric_key_prefix=metric_key_prefix,
        # )

        # n_samples = len(eval_dataset if eval_dataset is not None else self.eval_dataset)
        # output.metrics.update(speed_metrics(metric_key_prefix, start_time, n_samples))
        # self.log(output.metrics)

        # if self.args.tpu_metrics_debug or self.args.debug:
        #     # tpu-comment: Logging debug metrics for PyTorch/XLA (compile, execute times, ops, etc.)
        #     xm.master_print(met.metrics_report())

        # self.control = self.callback_handler.on_evaluate(self.args, self.state, self.control, output.metrics)
        # return output.metrics
