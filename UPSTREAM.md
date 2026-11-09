# 上游同步说明

本仓库是 [SiverKing/SiverWXbot_plus](https://github.com/SiverKing/SiverWXbot_plus) 的独立副本（未使用 GitHub fork，但保留了上游完整提交历史），在此基础上做少量必要修改。

- 上游仓库：https://github.com/SiverKing/SiverWXbot_plus
- 当前已同步到：v4.8.3（上游提交 `a861923`）

## 原则：机器人尽量保持原版

机器人只管微信收发。「回不回、怎么回、用哪个智能体」这类规则都放在自建网关
[lkyxuan/wechat-gateway](https://github.com/lkyxuan/wechat-gateway) 里（群规则 `chats`），
不要再改进机器人。这样上游更新时冲突最少，直接合并即可。

## 同步上游新版本

```bash
git remote add upstream https://github.com/SiverKing/SiverWXbot_plus.git   # 首次
git fetch upstream
git log --oneline HEAD..upstream/main      # 查看上游新增提交
git diff HEAD...upstream/main              # 阅读上游改动
git checkout -b sync/upstream-<版本号>
git merge upstream/main                    # 解决冲突后提 PR 合入 main
```

合并时保留本地修改（清单见 NOTICE），同步后更新本文件里的“当前已同步到”。
