# 上游同步说明

本仓库是 [SiverKing/SiverWXbot_plus](https://github.com/SiverKing/SiverWXbot_plus) 的独立副本（未使用 GitHub fork，但保留了上游完整提交历史），在此基础上二次开发。

- 上游仓库：https://github.com/SiverKing/SiverWXbot_plus
- 当前已同步到：v4.8.1（上游提交 `5f2f773`）

## 同步上游新版本

```bash
git remote add upstream https://github.com/SiverKing/SiverWXbot_plus.git   # 首次
git fetch upstream
git log --oneline HEAD..upstream/main      # 查看上游新增提交
git diff HEAD...upstream/main              # 阅读上游改动
git checkout -b sync/upstream-<版本号>
git merge upstream/main                    # 解决冲突后提 PR 合入 main
```

合并时注意保留本地修改（见 NOTICE）：
- `SIVER_HOST` 监听地址、关闭在线检查更新；
- 不引入 `siver_panel.py`（作者服务器中转客户端）；
- 同步后更新本文件里的“当前已同步到”。
