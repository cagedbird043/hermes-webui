# CagedBird 补丁栈

本 fork 不跟主线，以上游每个 tag 为基准维护一组补丁：

- 分支 `cb/<上游tag>`，例如 `cb/exp-v0.52.448`，= 上游 tag + 本组补丁。
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
old=exp-v0.52.448 new=<新tag>
git switch -c cb/$new $new
git cherry-pick $old..cb/$old          # 冲突逐个解决
git range-diff $old..cb/$old $new..cb/$new
.venv/bin/python -m pytest -q tests/test_native_image_attachments.py
git push -u origin cb/$new
```

本地测试环境：`uv venv --python 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt pillow`。

## 部署（sjc-edge）

源码在 `/home/hermes/hermes-webui-src`，由 hermes 用户的 `hermes-webui.service`（podman quadlet，用 Hermes 镜像内的 venv 运行）加载。

```sh
ssh sjc-edge
sudo -u hermes git -C /home/hermes/hermes-webui-src fetch origin
sudo -u hermes git -C /home/hermes/hermes-webui-src switch cb/<新tag>
u=$(id -u hermes)
sudo -u hermes XDG_RUNTIME_DIR=/run/user/$u DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$u/bus \
  systemctl --user restart hermes-webui
```

回滚：`switch` 回上一个 `cb/` 分支后重启。
