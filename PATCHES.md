# CagedBird 补丁栈

本 fork 不跟主线，以上游每个 tag 为基准维护一组补丁：

- 分支 `cb/<上游tag>`，例如 `cb/exp-v0.52.543`，= 上游 tag + 本组补丁。
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
old=exp-v0.52.460 new=exp-v0.52.543  # 按实际基线填写
git switch -c cb/$new cb/$old
git rebase --onto $new $old          # 冲突逐个解决
git range-diff $old..cb/$old $new..cb/$new
.venv/bin/python -m pytest -q tests/test_native_image_attachments.py tests/test_user_provider_endpoint_discovery.py tests/test_custom_provider_display_name.py
git push -u origin cb/$new
```

本地测试环境：`uv venv --python 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt pillow`。

## 部署（sjc-edge）

源码在 `/home/hermes/hermes-webui-src`，由 hermes 用户的 `hermes-webui.service`（podman quadlet，用 Hermes 镜像内的 venv 运行）加载。

部署前检查 `/health` 的 `active_streams` 与 `active_runs` 为 0，再停服务并备份完整 `.hermes` 数据（含 `state.db`、`sessions`、`webui` 和附件），备份放在数据目录以外并限制访问权限。Hermes Agent 镜像保持不变。

```sh
ssh sjc-edge
sudo -u hermes git -C /home/hermes/hermes-webui-src fetch origin
sudo -u hermes git -C /home/hermes/hermes-webui-src switch cb/<新tag>
u=$(id -u hermes)
sudo -u hermes XDG_RUNTIME_DIR=/run/user/$u DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$u/bus \
  systemctl --user restart hermes-webui
```

回滚：停服务后切回旧 `cb/` 分支，必要时从升级前的数据备份恢复，再启动。`exp-v0.52.534` 起有运行日志压缩归档；回滚到不识别归档格式的版本前须同时考虑数据恢复，不能只切代码分支。
