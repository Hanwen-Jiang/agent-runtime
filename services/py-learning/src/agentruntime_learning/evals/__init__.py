"""评测集与评测 harness（计划 S-08；设计书附录 B.3–B.7、C.1–C.5、J.3）。

Planner、Reviewer 与压缩提示都靠评测迭代（S-09、S-10、S-11），所以先有评测集，再写提示。

- 评测集（cases）：Task 用例与 Reviewer 专用样本，每个文件一条，格式见 case.schema.json；标注要经人确认，
  分开发集与留出集。默认的评测集在 services/py-learning/evalsets/task-loop。
- 被测对象（model.Subject）：plan 与 review 两个方法，对应 Round 开头的 Planner 与结尾的 Reviewer。
  Reviewer 的输入只有 Stage、Stage 结果与 Task State（含验收条件），不含 Planner 的规划过程。
- 评分器（scoring、rubric）：结构校验（/schemas 与 DAG 检查，必须 100% 通过）、规则检查（由代码判定）、
  形状特征、Verdict 与升级顺序；rubric 评分用与被测模型不同家的模型。
- 运行器（runner、__main__）：一条命令跑完，输出成功率、成本，以及与上一版的差异；留出集只给汇总分数。
"""
