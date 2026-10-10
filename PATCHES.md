# CagedBird 补丁栈

本 fork 不跟主线，只以上游稳定 tag（`vX.Y.Z`，不含 `exp-`）为基准维护一组补丁：

- 分支 `cb/<上游tag>`，例如 `cb/v0.52.544`，= 上游 tag + 本组补丁。
- 每个补丁一个提交，一件事。旧分支保留，回滚即切回旧分支。
- `master` 不使用。

## 补丁

| 补丁 | 说明 |
|---|---|
| 原生图片嵌入前缩到长边 2048 | `api/streaming.py` `_downscale_for_native_embed`：按 EXIF 转正，长边超过 2048 或原文件超过 3 MiB 时重编码为 JPEG（质量 85，带透明通道则为 PNG）；SVG、GIF 与 Pillow 无法解码的文件原样发送。 |
| providers 自定义端点实时发现模型 | `api/config.py` `_user_provider_probe_base_url` / `_user_provider_api_key`：`providers.<id>` 条目没有写死 `models` 且未关闭 `discover_models` 时，按 `base_url`/`url`/`api` 与 `key_env` 探测 `/v1/models`（`hermes_cli.provider_model_ids` 只认内置 provider，原先这类分组只剩默认模型）。 |
| 本文件 | 补丁栈说明。 |

## 升级到新 tag

```sh
git fetch upstream --tags
old=v0.52.544 new=v0.52.5xx  # 按实际基线填写
git switch -c cb/$new cb/$old
git rebase --onto $new $old          # 冲突逐个解决
git range-diff $old..cb/$old $new..cb/$new
.venv/bin/python -m pytest -q tests/test_native_image_attachments.py tests/test_user_provider_endpoint_discovery.py tests/test_custom_provider_display_name.py
git push -u origin cb/$new
```

本地测试环境：`uv venv --python 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt pillow`。

## 部署（sjc-edge）

源码在 `/home/hermes/hermes-webui-src`，由 hermes 用户的 `hermes-webui.service`（podman quadlet，用 Hermes 镜像内的 venv 运行）加载。

日常升级已自动化：fork 默认分支 `cb/automation` 里的 `cb-follow` workflow 每天变基到上游最新稳定 tag 并推送新 `cb/` 分支；sjc-edge 的 `hermes-autoupdate.timer` 每天 04:00 拉取新分支与新 Agent 镜像，等空闲、切换、健康检查，失败自动回滚，并通过 Telegram 通知。细节见该分支的 `README.md`。

Hermes Agent 镜像与 WebUI 一起升级：`hermes-webui` 与 `hermes-gateway` 两个 quadlet 都写具体版本标签（如 `docker.io/nousresearch/hermes-agent:v0.22.5`），不用会移动的 `stable`。只升级 WebUI 时不备份数据；Agent 换版本前只快照 `.hermes/state.db` 与 `config.yaml` 到 `~/.local/state/hermes-autoupdate/agent-snapshot/`（仅保留最近一份）。

手动部署（自动化停用或需要人工处理时）：先确认 `/health` 的 `active_streams` 与 `active_runs` 为 0。

```sh
ssh sjc-edge
sudo -u hermes git -C /home/hermes/hermes-webui-src fetch origin
sudo -u hermes git -C /home/hermes/hermes-webui-src switch cb/<新tag>
u=$(id -u hermes)
sudo -u hermes XDG_RUNTIME_DIR=/run/user/$u DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$u/bus \
  systemctl --user restart hermes-webui
```

回滚：停服务后切回旧 `cb/` 分支；若 Agent 也换了版本，把两个 quadlet 改回原镜像标签，并从 `agent-snapshot/` 恢复 `state.db` 与 `config.yaml`（同时删除 `state.db-wal`、`state.db-shm`），再启动。`exp-v0.52.534` 起有运行日志压缩归档；回滚到不识别归档格式的版本前须同时考虑数据恢复，不能只切代码分支。
