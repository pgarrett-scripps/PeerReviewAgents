# A Lightweight Method for Estimating Widget Throughput

## Abstract
We propose WidgetNet, a method for estimating throughput in distributed widget
pipelines. We claim a 30% improvement over prior art on three benchmarks.

## Introduction
Widget throughput estimation is important. Prior methods are slow. We make it fast.

## Methods
We train a small model on logs from a single production cluster. We compare against
two baselines using mean absolute error.

## Results
WidgetNet achieves lower error on all three datasets (p < 0.05). See Figure 1.

## Discussion
Our method generalizes broadly to all widget systems.

## Conclusion
WidgetNet is fast and accurate.

## References
[1] Smith et al., Widgets, 2019.
