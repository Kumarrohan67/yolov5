import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from tqdm import tqdm
import torch

sys.path.append(str(Path(__file__).parent.parent.parent))  # add utils/ to path
from utils.general import colorstr, xywh2xyxy
from utils.datasets import img2label_paths

try:
    import wandb
except ImportError:
    wandb = None
    print(f"{colorstr('wandb: ')}Install Weights & Biases for YOLOv5 logging with 'pip install wandb' (recommended)")

WANDB_ARTIFACT_PREFIX = 'wandb-artifact://'


def remove_prefix(from_string, prefix):
    return from_string[len(prefix):]


class WandbLogger():
    def __init__(self, opt, name, run_id, data_dict, job_type='Training'):
        self.wandb = wandb
        self.wandb_run = wandb.init(config=opt, resume="allow",
                                    project='YOLOv5' if opt.project == 'runs/train' else Path(opt.project).stem,
                                    name=name,
                                    job_type=job_type,
                                    id=run_id) if self.wandb else None

        if job_type == 'Training':
            self.setup_training(opt, data_dict)
            self.current_epoch = 0
            if opt.bbox_interval == -1:
                opt.bbox_interval = (opt.epochs // 10) if opt.epochs > 10 else opt.epochs

    def setup_training(self, opt, data_dict):
        self.log_dict = {}
        self.train_artifact_path, self.trainset_artifact = \
            self.download_dataset_artifact(data_dict['train'], opt.artifact_alias)
        self.test_artifact_path, self.testset_artifact = \
            self.download_dataset_artifact(data_dict['val'], opt.artifact_alias)
        self.result_artifact, self.result_table, self.weights = None, None, None
        if self.train_artifact_path is not None:
            train_path = Path(self.train_artifact_path) / 'data/images/'
            data_dict['train'] = str(train_path)
        if self.test_artifact_path is not None:
            test_path = Path(self.test_artifact_path) / 'data/images/'
            data_dict['val'] = str(test_path)
            self.result_artifact = wandb.Artifact("run_" + wandb.run.id + "_progress", "evaluation")
            self.result_table = wandb.Table(["epoch", "id", "prediction", "avg_confidence"])
        if opt.resume_from_artifact:
            modeldir, _ = self.download_model_artifact(opt.resume_from_artifact)
            if modeldir:
                self.weights = Path(modeldir) / "best.pt"
                opt.weights = self.weights

    def download_dataset_artifact(self, path, alias):
        if path.startswith(WANDB_ARTIFACT_PREFIX):
            dataset_artifact = wandb.use_artifact(remove_prefix(path, WANDB_ARTIFACT_PREFIX) + ":" + alias)
            assert dataset_artifact is not None, "'Error: W&B dataset artifact doesn\'t exist'"
            datadir = dataset_artifact.download()
            labels_zip = Path(datadir) / "data/labels.zip"
            shutil.unpack_archive(labels_zip, Path(datadir) / 'data/labels', 'zip') if labels_zip.exists() else None
            return datadir, dataset_artifact
        return None, None

    def download_model_artifact(self, name):
        model_artifact = wandb.use_artifact(name + ":latest")
        assert model_artifact is not None, 'Error: W&B model artifact doesn\'t exist'
        modeldir = model_artifact.download()
        return modeldir, model_artifact

    def log_model(self, path, opt, epoch):
        datetime_suffix = datetime.today().strftime('%Y-%m-%d-%H-%M-%S')
        model_artifact = wandb.Artifact('run_' + wandb.run.id + '_model', type='model', metadata={
            'original_url': str(path),
            'epoch': epoch + 1,
            'save period': opt.save_period,
            'project': opt.project,
            'datetime': datetime_suffix
        })
        model_artifact.add_file(str(path / 'last.pt'), name='last.pt')
        model_artifact.add_file(str(path / 'best.pt'), name='best.pt')
        wandb.log_artifact(model_artifact)
        print("Saving model artifact on epoch ", epoch + 1)

    def log_dataset_artifact(self, dataset, class_to_id, name='dataset'):
        artifact = wandb.Artifact(name=name, type="dataset")
        for img_file in [dataset.path] if Path(dataset.path).is_dir() else dataset.img_files:
            if Path(img_file).is_dir():
                artifact.add_dir(img_file, name='data/images')
            else:
                artifact.add_file(img_file, name='data/images/'+Path(img_file).name)
                label_file = Path(img2label_paths([img_file])[0])
                artifact.add_file(str(label_file), name='data/labels/'+label_file.name) if label_file.exists() else None
        table = wandb.Table(columns=["id", "train_image", "Classes"])
        class_set = wandb.Classes([{'id': id, 'name': name} for id, name in class_to_id.items()])
        for si, (img, labels, paths, shapes) in enumerate(tqdm(dataset)):
            height, width = shapes[0]
            labels[:, 2:] = (xywh2xyxy(labels[:, 2:].view(-1, 4))) * torch.Tensor([width, height, width, height])
            box_data, img_classes = [], {}
            for cls, *xyxy in labels[:, 1:].tolist():
                cls = int(cls)
                box_data.append({"position": {"minX": xyxy[0], "minY": xyxy[1], "maxX": xyxy[2], "maxY": xyxy[3]},
                                 "class_id": cls,
                                 "box_caption": "%s" % (class_to_id[cls]),
                                 "scores": {"acc": 1},
                                 "domain": "pixel"})
                img_classes[cls] = class_to_id[cls]
            boxes = {"ground_truth": {"box_data": box_data, "class_labels": class_to_id}}  # inference-space
            table.add_data(si, wandb.Image(paths, classes=class_set, boxes=boxes), json.dumps(img_classes))
        artifact.add(table, name)
        if Path(dataset.path).is_dir():
            labels_path = 'labels'.join(dataset.path.rsplit('images', 1))
            zip_path = Path(labels_path).parent / (name + '_labels.zip')
            if not zip_path.is_file():  # make_archive won't check if file exists
                shutil.make_archive(zip_path.with_suffix(''), 'zip', labels_path)
            artifact.add_file(str(zip_path), name='data/labels.zip')
        wandb.log_artifact(artifact)

    def log(self, log_dict):
        if self.wandb_run:
            for key, value in log_dict.items():
                self.log_dict[key] = value

    def end_epoch(self, best_result=False):
        if self.wandb_run and self.log_dict:
            wandb.log(self.log_dict)
            self.log_dict = {}
        if self.result_artifact:
            train_results = wandb.JoinedTable(self.testset_artifact.get("val"), self.result_table, "id")
            self.result_artifact.add(train_results, 'result')
            wandb.log_artifact(self.result_artifact, aliases=['best'] if best_result else None)
            self.result_table = wandb.Table(["epoch", "id", "prediction", "avg_confidence"])
            self.result_artifact = wandb.Artifact("run_" + wandb.run.id + "_progress", "evaluation")

    def finish_run(self):
        if self.wandb_run:
            if self.log_dict:
                wandb.log(self.log_dict)
            wandb.run.finish()
