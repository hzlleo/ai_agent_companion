# 模型子模组（models/）

本地辅助模型以 **git 子模组**形式挂载（优先 ModelScope 源，国内下载友好）。主仓库只记录 gitlink 与 `.gitmodules`——**权重经 git-lfs 按需拉取**，克隆主仓库默认零权重下载，测试全部密封（CI 不拉子模组）。

## 模型清单

| 子模组路径 | 源（ModelScope） | 用途 | 服务形态（部署机） |
|---|---|---|---|
| `models/Qwen3-VL-4B-Instruct-GGUF` | Qwen/Qwen3-VL-4B-Instruct-GGUF | `look_at` 视觉理解（图片/截图/文档 → 中文结构化描述） | llama-server（OpenAI 兼容含图端点，Q4_K_M + mmproj） |
| `models/Qwen3-Embedding-0.6B-GGUF` | Qwen/Qwen3-Embedding-0.6B-GGUF | 记忆检索向量（embed 角色） | llama-server `--embedding` |
| `models/Qwen3-ASR-0.6B` | Qwen/Qwen3-ASR-0.6B | 语音识别（22 方言流式转写） | vLLM 工具包（OpenAI 兼容 `/v1/audio/transcriptions`；本服务钉 `vllm==0.14.0`，与远端主 LLM 的 0.32+ 互不影响） |
| `models/Fun-CosyVoice3-0.5B-2512` | FunAudioLLM/Fun-CosyVoice3-0.5B-2512 | 语音合成（指令切方言/音色克隆） | CosyVoice 官方运行时（`POST /tts` → wav 流） |

权重体积以各仓库页面为准（合计约 8–10 GB；`git lfs pull` 按子模组逐个拉取）。

## 首次获取

```bash
# 1) 克隆主仓库（含子模组骨架，不下载权重）
git clone --recurse-submodules https://github.com/hzlleo/ai_agent_companion.git
# 已有克隆补装子模组：
git submodule update --init

# 2) ⚠️ git ≥ 2.5x 的 UA 会被 ModelScope 网关以 421 拒绝（已实证）——加域限定覆写：
git config --global http.https://www.modelscope.cn/.userAgent "git/2.47.0"

# 3) 部署机拉取真实权重（需要 git-lfs）
git lfs install
for m in models/*; do (cd "$m" && git lfs pull); done
```

注：第 2 步仅影响 modelscope.cn 域；`git submodule add/update` 若克隆失败，先检查该配置。

## 升级模型版本

```bash
cd models/<子模组>
git fetch --depth 1 origin main && git checkout origin/main   # 或指定 tag
cd ../..
git add models/<子模组>    # 记录新 gitlink
```

## CI 说明

CI 全部测试密封（MockTransport/fake），**不拉取子模组**——`.gitmodules` 存在即满足，checkout 默认跳过。

## 与后端配置的关系

各模型服务以 HTTP 端点进入后端配置（llm 注册表条目 / voice 配置节 / tools_sandbox 白名单），子模组路径仅为部署机上的权重挂载源（compose 卷挂载 `./models/...`）；后端代码不读取子模组路径。
