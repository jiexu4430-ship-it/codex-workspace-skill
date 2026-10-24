# codex-workspace-skill

让 Codex 为每个对话建立并持续复用一个文件工作区。目录跟随主对话名称，分叉默认共用，文件按需简单分类。

A local-first Codex Skill for Windows: one persistent workspace per conversation, owner-controlled renaming, and shared workspaces for forks. Python standard library only.

## 能做什么

- 首次调用按对话标题建立目录，再次调用复用已有绑定。
- 主对话改名后，在下次调用或文件任务开始时核对并同步目录名。
- 分叉共用原工作区，分叉自己的标题不会改变原目录。
- 原目录失踪时先询问；手动移动后经过确认再修正绑定。
- 发生同名冲突、损坏记录、重复归属或操作占用时停止，不覆盖或合并已有目录。
- 文件少时直接放根目录，需要时再分为 `资料`、`代码`、`成果`、`临时` 等少量类别。

工作区归属写在目录内的 `.chat-workspace.json`，不使用目录外的全局注册表，不修改 Codex 历史数据库，不保存完整聊天正文。

## 运行要求

- Windows。
- Python 3.10 或更新版本，无需安装 pip 包。
- 能加载本地 Skill 的 Codex 环境。
- 能够可靠取得当前对话的编号和标题。环境未提供时，Skill 会请求核实，不猜测最近的聊天。

以下命令使用 Windows 的 `py -3` 启动器。如果没有它，请替换为你已验证可运行的 Python 3.10+ 解释器。

## 安装

在仓库页面下载源码并解压，或克隆本仓库；在源码根目录打开 PowerShell。

1. 复制配置示例并编辑：

   ```powershell
   Copy-Item .\workspace\config.example.json .\workspace\config.json
   notepad .\workspace\config.json
   ```

   将 `allowed_roots` 改成你已有的主目录。允许配置一个或多个目录，不要求固定数量。示例：

   ```json
   {
     "schema_version": 1,
     "allowed_roots": ["D:\\Workspaces"]
   }
   ```

   路径必须是绝对路径、目录必须已经存在，并且不能经过符号链接或 Windows 联接点。示例不会自动创建主目录，也不会自动授予访问权限。实际工作区是这些主目录的直接子目录。

2. 执行安装：

   ```powershell
   py -3 -B -X utf8 .\install.py --config .\workspace\config.json
   ```

   默认安装到当前用户的 `~/.codex/skills/workspace`；若设置了 `CODEX_HOME`，则使用该目录下的 `skills/workspace`。安装器会生成配置并逐文件校验，不修改系统权限或聊天历史。

   同样的内容重复安装只做检查；遇到不同内容的已有 Skill 就停止，不覆盖你的个人修改。升级或替换需单独备份、核对并明确决定。

3. 如果技能列表没有刷新，重新打开 Codex，再在对话中输入：

   ```text
   $workspace
   ```

本地 `workspace/config.json` 已被 `.gitignore` 排除。发布仓库或提交修改时仅保留 `config.example.json`。

## 日常使用

```text
使用 $workspace，为当前对话初始化工作区。
```

再次调用会检查并复用。主对话更名后，再调用一次即可核对目录名称；无法安全改名时会说明原因。

只检查、不修改：

```text
使用 $workspace 检查当前工作区，不修改文件。
```

如果原目录找不到，可以选择提供新位置、手动创建后告知路径，或明确授权重新创建。重新创建空目录不代表找回旧文件。

## 项目结构

```text
codex-workspace-skill/
├── README.md
├── LICENSE
├── TESTING.md
├── install.py
├── workspace/
│   ├── SKILL.md
│   ├── config.example.json
│   ├── agents/openai.yaml
│   └── scripts/workspace.py
├── tests/
│   ├── test_workspace.py
│   └── test_install.py
└── tools/package_source.py
```

`install.py --destination <绝对路径>` 可用于明确指定另一个名为 `workspace` 的安装目录，例如隔离测试；这不会改变 Codex 的技能搜索设置。日常安装使用默认位置即可。

helper 的命令包括 `context`、`status`、`init`、`bind`、`share`、`rename`、`recover`、`check-path` 和 `unlock`。详细流程见 [SKILL.md](workspace/SKILL.md)。helper 输出 UTF-8 JSON，失败退出码为 `2`。

## 测试与打包

在 Windows 上执行：

```powershell
py -3 -B -X utf8 -m unittest discover -s tests -v
py -3 -B -X utf8 .\tools\package_source.py
```

打包结果在 `dist/codex-workspace-skill-source.zip`，旁边附有 SHA-256 校验文件。打包使用固定的源码清单，即使存在本地配置或聊天绑定记录也不会收进去。压缩包内另有逐文件的 `SHA256SUMS.txt`。

测试和实际限制见 [TESTING.md](TESTING.md)。

## 边界

- 这是模型操作规则加目录管理脚本，不是系统级沙箱，也不会后台监听标题变化。
- 对话 ID、标题、分叉来源取决于运行环境。无法确认时停止核实；不保证所有客户端版本都提供相同信息。
- 没有外部注册表。如果目录、标记和可见历史线索全部丢失，无法可靠区分“从未创建”和“以前的目录已失踪”。
- 锁仅保护单个目录的管理操作。多个窗口不要同时给同一对话首次创建不同目录或加入不同共享；共用文件的并行编辑也需要协调。重复绑定会在后续扫描时被拒绝。
- 改名前要检查正在使用目录的软件和工程路径依赖。第三方软件自身缓存不一定能留在工作区内。
- 归档聊天、删除聊天或卸载 Skill，不会自动删除成果目录。

## 发布与许可

可直接发布本目录中的源码文件；仓库推荐名称为 `codex-workspace-skill`。不要把个人工作区、安装后的本地配置或测试临时目录一并上传。发布前可先运行测试及源码打包命令。

本项目采用 [MIT License](LICENSE)。版权声明使用项目贡献者署名，不包含个人电脑的用户名或路径。
