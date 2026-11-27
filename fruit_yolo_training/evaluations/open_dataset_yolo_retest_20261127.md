# YOLO 开源水果数据集复测

日期：2026-11-27

## 结论

同一留出测试集上的复测显示，当前应用使用的 COCO 预训练 `yolov8n.pt` 整体优于五轮水果训练候选。候选权重没有通过本次测试，不建议替换当前应用权重。

## 数据和方法

- 开源数据：[`lightly-ai/dataset_fruits_detection`](https://github.com/lightly-ai/dataset_fruits_detection)，许可证 CC0-1.0；本机镜像提交 `e626559d4a127ea9fc35fedd74c52ee9f378a53d`。
- 使用该项目固定随机种子 `20261125` 生成的独立 `test` 子集：280 张图、914 个水果框，类别为 apple、banana、grape、orange、pineapple、watermelon。不是上游完整 8,479 张数据集。
- 两个模型分别在完全相同的数据、阈值 `conf=0.35`、输入边长 640、CPU 上评估；同类别 IoU≥0.50 的一对一匹配计为 TP。
- 召回率（本项目“命中率”）=`TP/(TP+FN)`；精确率=`TP/(TP+FP)`；F1 为两者调和平均。该指标不是 COCO 官方 mAP。
- 未训练或修改模型，也未将候选权重部署到相机服务。

## 结果

| 模型 | 标注框 | TP | FP | FN | 召回率/命中率 | 精确率 | F1 | 280 张推理耗时 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 当前应用：COCO YOLOv8n | 914 | 212 | 353 | 702 | 23.19% | 37.52% | 28.67% | 38.27 秒 |
| 水果候选：YOLOv8n，5 epochs | 914 | 126 | 441 | 788 | 13.79% | 22.22% | 17.02% | 43.02 秒 |

| 类别 | 当前应用召回率 | 五轮水果候选召回率 |
|---|---:|---:|
| 苹果 | 45.51% | 26.28% |
| 香蕉 | 34.95% | 12.62% |
| 葡萄 | 0.00% | 16.16% |
| 橙子 | 35.71% | 10.20% |
| 菠萝 | 0.00% | 8.05% |
| 西瓜 | 0.00% | 3.95% |

当前 COCO 权重在 3 个已支持类别上更好，但不支持葡萄、菠萝和西瓜；候选虽然能检出部分新增类别，整体召回、精确率与 F1 均下降。此数据不足以证明候选适合现场分拣。

## 可复核产物

- 当前应用权重逐图结果：`current_coco_openfruit_20261127_test_conf_0.35.json`
- 水果候选逐图结果：`fruit_candidate_openfruit_20261127_test_conf_0.35.json`
- 数据划分清单：`../dataset_subset/subset_manifest.json`
- 当前权重 SHA-256：`F59B3D833E2FF32E194B5BB8E08D211DC7C5BDF144B90D2C8412C47CCFC83B36`
- 候选权重 SHA-256：`4704F069340F2A243C18783311A09DAB56FAAB2CD61043323A8DFAB74E83519A`

复现命令（在 `fruit_yolo_training` 目录运行）：

```powershell
& 'C:\Python310\python.exe' .\evaluate_split.py --model '..\yolov8n.pt' --data .\dataset_subset\data.yaml --split test --conf 0.35 --iou 0.50 --imgsz 640 --device cpu --tag current_coco_openfruit_20261127
& 'C:\Python310\python.exe' .\evaluate_split.py --model .\candidates\fruit_candidate_yolov8n_20261125.pt --data .\dataset_subset\data.yaml --split test --conf 0.35 --iou 0.50 --imgsz 640 --device cpu --tag fruit_candidate_openfruit_20261127
```

## 验收边界

这是固定留出图片集上的离线检测评测，不等同于本机 DaBai DCW 画面、不同光照/距离下的实物准确率，也不等同于机械臂抓取成功率。当前摆放的字母 `D` 曾被 OCR 实机读出，但它不是水果检测正样本。
