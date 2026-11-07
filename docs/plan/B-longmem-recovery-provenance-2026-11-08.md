# B：LongMem恢复账本来源验收

本项与A并行，A拥有恢复执行器与付费运行，B仅独立验证恢复合同。基于A提交2dd5ddd3；没有修改A工作树、正在运行的控制脚本、线上部署或账本。

冻结helper原SHA256 b1b8487cf32bacec3664ba294f61ef1da23b15e7de1ea09420b326f8778b4917。原17项通过；新增4条回归在原实现均失败：恢复期间新增已结算attempt即使Registry版本v99、scope改变、正文digest改变或身份全部NULL，finalize仍接受。

修复在verify_ledger逐新增attempt核对原freeze里的platform/model_profile版本与正文摘要。出现新增账单却未传frozen_registry时拒绝。prepare/finalize都从原冻结文件传递，不能用当前Registry最新版本替换。旧账本逐行不变、exact pending身份/152micros保留、新pending停止、原20美元预算和已完成题不重跑规则不变。

修后22项通过（约0.3s）：原17项加4种身份漂移拒绝及匹配身份成功/未知费仍未知。无模型调用、无线上写入、无重新评测。

当前A的控制器 /opt/evals/longmemeval-recovery-control-20261107/recover.py 已在每题和终态调用原launch.observe，后者validate_registry会逐attempt验证。因此没有证据表明当前运行已受版本漂移污染，勿打断或热替换它。本补丁使独立helper/CLI也拒绝失配，供当前run结束后集成。控制器以后调用verify_ledger且存在新增rows时，应传 frozen_registry=original_freeze[deployment][registry]；不修改冻结的原检索器源码。

证据：父目录.goal-tools/B-longmem-closeout-20261108/{snapshot.json,baseline-tests.log,provenance-red.log,provenance-green.log,final-tests.log}。原36已完成/14从未启动已按完整journal复核，A正在执行唯一14题恢复进程；B不额外运行付费题。
