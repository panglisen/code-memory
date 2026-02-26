#!/bin/bash
# install.sh - code-memory 一键安装脚本
# 用法: bash install.sh [--uninstall]
#
# 安装内容:
#   1. 脚本 → ~/.claude/scripts/
#   2. 斜杠命令 → ~/.claude/commands/
#   3. 加载规则 → ~/.claude/rules/
#   4. 记忆目录结构 → ~/.claude/memory/
#   5. 模板文件 → ~/.claude/memory/ (不覆盖已有)
#   6. Hooks → ~/.claude/settings.json (合并，不覆盖已有配置)

set -e

# --- 颜色 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- 路径 ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
COMMANDS_DIR="$CLAUDE_DIR/commands"
RULES_DIR="$CLAUDE_DIR/rules"
MEMORY_DIR="$CLAUDE_DIR/memory"

# --- 辅助函数 ---
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[SKIP]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

copy_file() {
    local src="$1" dest="$2"
    cp "$src" "$dest"
    ok "$(basename "$dest")"
}

copy_if_not_exists() {
    local src="$1" dest="$2"
    if [ -f "$dest" ]; then
        warn "$(basename "$dest") 已存在，跳过 (不覆盖)"
    else
        cp "$src" "$dest"
        ok "$(basename "$dest") (从模板创建)"
    fi
}

# --- 卸载 ---
if [ "$1" = "--uninstall" ]; then
    echo ""
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  code-memory 卸载${NC}"
    echo -e "${RED}========================================${NC}"
    echo ""

    # 脚本
    for f in memory-search.py session-summary.sh extract-memory.sh \
             weekly-consolidate.sh auto-extract-facts.py extract-schema.py \
             migrate-sessions.sh memory-feedback.py signal-analyzer.py \
             capability-generator.py cleanup-avoidances.sh; do
        if [ -f "$SCRIPTS_DIR/$f" ]; then
            rm "$SCRIPTS_DIR/$f"
            ok "删除 scripts/$f"
        fi
    done
    [ -f "$SCRIPTS_DIR/lib/extract-conversation.jq" ] && rm "$SCRIPTS_DIR/lib/extract-conversation.jq" && ok "删除 scripts/lib/extract-conversation.jq"

    # 斜杠命令
    for f in memory-add.md memory-learn.md memory-avoid.md memory-summarize.md \
             memory-health.md memory-signals.md memory-generate.md; do
        if [ -f "$COMMANDS_DIR/$f" ]; then
            rm "$COMMANDS_DIR/$f"
            ok "删除 commands/$f"
        fi
    done

    # 规则
    for f in memory-loader.md auto-capabilities.md; do
        if [ -f "$RULES_DIR/$f" ]; then
            rm "$RULES_DIR/$f"
            ok "删除 rules/$f"
        fi
    done

    echo ""
    warn "记忆数据 (~/.claude/memory/) 已保留，如需删除请手动执行:"
    echo "  rm -rf ~/.claude/memory"
    warn "settings.json 中的 hooks 需手动移除"
    echo ""
    echo -e "${GREEN}卸载完成${NC}"
    exit 0
fi

# --- 安装 ---
echo ""
echo "=========================================="
echo "  code-memory 安装"
echo "=========================================="
echo ""

# --- 检查依赖 ---
info "检查依赖..."

HAS_PYTHON=false
HAS_JQ=false
PYTHON_CMD=""

if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
    HAS_PYTHON=true
    PYTHON_VER=$(python3 --version 2>&1 | awk '{print $2}')
    ok "python3 ($PYTHON_VER)"

    # 检查 Python 版本 >= 3.9
    PYTHON_MAJOR=$(echo "$PYTHON_VER" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VER" | cut -d. -f2)
    if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]); then
        err "Python 3.9+ 必需，当前版本 $PYTHON_VER"
        exit 1
    fi
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
    HAS_PYTHON=true
    ok "python ($(python --version 2>&1 | awk '{print $2}'))"
fi

if ! $HAS_PYTHON; then
    err "Python 3.9+ 未安装 (必需)"
    echo "  安装: https://www.python.org/downloads/"
    exit 1
fi

if command -v jq &>/dev/null; then
    HAS_JQ=true
    ok "jq ($(jq --version 2>&1))"
else
    warn "jq 未安装 (会话摘要功能需要)"
    echo "  安装: brew install jq 或 apt install jq"
fi

if command -v claude &>/dev/null; then
    ok "claude CLI"
else
    warn "claude CLI 未安装 (会话摘要的 LLM 提炼需要)"
    echo "  安装: npm install -g @anthropic-ai/claude-code"
fi

# 可选依赖
echo ""
info "检查可选依赖..."
if $HAS_PYTHON && $PYTHON_CMD -c "import fastembed" 2>/dev/null; then
    ok "fastembed (向量搜索已启用)"
else
    warn "fastembed 未安装 (搜索将使用纯 BM25 模式)"
    echo "  可选安装: pip install fastembed"
fi
echo ""

# --- 1. 创建目录结构 ---
info "创建目录结构..."

mkdir -p "$SCRIPTS_DIR/lib"
mkdir -p "$COMMANDS_DIR"
mkdir -p "$RULES_DIR"
mkdir -p "$MEMORY_DIR"/{daily,sessions,evolution,areas/{projects,patterns,tools}}

ok "目录结构就绪"
echo ""

# --- 2. 复制脚本 ---
info "安装脚本 → ~/.claude/scripts/"

for f in memory-search.py session-summary.sh extract-memory.sh \
         weekly-consolidate.sh auto-extract-facts.py extract-schema.py \
         migrate-sessions.sh memory-feedback.py signal-analyzer.py \
         capability-generator.py cleanup-avoidances.sh; do
    copy_file "$SCRIPT_DIR/scripts/$f" "$SCRIPTS_DIR/$f"
done
copy_file "$SCRIPT_DIR/scripts/lib/extract-conversation.jq" "$SCRIPTS_DIR/lib/extract-conversation.jq"

# 设置可执行权限
chmod +x "$SCRIPTS_DIR"/*.sh "$SCRIPTS_DIR"/*.py
ok "可执行权限已设置"
echo ""

# --- 3. 复制斜杠命令 ---
info "安装斜杠命令 → ~/.claude/commands/"

for f in memory-add.md memory-learn.md memory-avoid.md memory-summarize.md \
         memory-health.md memory-signals.md memory-generate.md; do
    copy_file "$SCRIPT_DIR/commands/$f" "$COMMANDS_DIR/$f"
done
echo ""

# --- 4. 复制加载规则 ---
info "安装加载规则 → ~/.claude/rules/"
copy_file "$SCRIPT_DIR/rules/memory-loader.md" "$RULES_DIR/memory-loader.md"
copy_file "$SCRIPT_DIR/rules/auto-capabilities.md" "$RULES_DIR/auto-capabilities.md"
echo ""

# --- 5. 初始化模板文件 ---
info "初始化模板文件 (已有文件不覆盖)..."

copy_if_not_exists "$SCRIPT_DIR/templates/MEMORY.md" "$MEMORY_DIR/MEMORY.md"
copy_if_not_exists "$SCRIPT_DIR/config/project-config.example.json" "$MEMORY_DIR/config.json"
echo ""

# --- 6. 合并 Hooks 到 settings.json ---
info "配置 Hooks → ~/.claude/settings.json"

SETTINGS_FILE="$CLAUDE_DIR/settings.json"

merge_hooks() {
    # 需要 jq 来合并 JSON
    if ! $HAS_JQ; then
        warn "jq 未安装，无法自动合并 hooks"
        echo "  请手动将以下内容合并到 $SETTINGS_FILE:"
        echo ""
        cat "$SCRIPT_DIR/config/settings.example.json"
        echo ""
        return
    fi

    if [ ! -f "$SETTINGS_FILE" ]; then
        # settings.json 不存在，直接复制
        cp "$SCRIPT_DIR/config/settings.example.json" "$SETTINGS_FILE"
        ok "settings.json 已创建 (含 hooks 配置)"
        return
    fi

    # 检查是否已有 code-memory 的 hooks
    if jq -e '.hooks.PostToolUse[]? | select(.hooks[]?.command | test("extract-memory"))' "$SETTINGS_FILE" &>/dev/null; then
        warn "Hooks 已配置，跳过"
        return
    fi

    # 合并 hooks: 将新 hooks 追加到已有 hooks 数组
    local TEMP_FILE
    TEMP_FILE=$(mktemp)

    jq --argjson new_hooks "$(cat "$SCRIPT_DIR/config/settings.example.json")" '
        # 确保 hooks 对象存在
        .hooks //= {} |

        # 合并 PostToolUse
        .hooks.PostToolUse = ((.hooks.PostToolUse // []) + ($new_hooks.hooks.PostToolUse // [])) |

        # 合并 Stop
        .hooks.Stop = ((.hooks.Stop // []) + ($new_hooks.hooks.Stop // [])) |

        # 合并 SessionEnd
        .hooks.SessionEnd = ((.hooks.SessionEnd // []) + ($new_hooks.hooks.SessionEnd // []))
    ' "$SETTINGS_FILE" > "$TEMP_FILE"

    if [ $? -eq 0 ] && [ -s "$TEMP_FILE" ]; then
        mv "$TEMP_FILE" "$SETTINGS_FILE"
        ok "Hooks 已合并到 settings.json"
    else
        rm -f "$TEMP_FILE"
        err "Hooks 合并失败，请手动配置"
        echo "  参考: $SCRIPT_DIR/config/settings.example.json"
    fi
}

merge_hooks
echo ""

# --- 7. 验证 ---
info "验证安装..."

ERRORS=0

# 检查关键文件
for f in scripts/memory-search.py scripts/session-summary.sh scripts/extract-memory.sh \
         scripts/memory-feedback.py scripts/signal-analyzer.py scripts/capability-generator.py; do
    if [ -f "$CLAUDE_DIR/$f" ]; then
        ok "$f"
    else
        err "$f 缺失"
        ERRORS=$((ERRORS + 1))
    fi
done

# 检查搜索脚本可运行
if $HAS_PYTHON && $PYTHON_CMD "$SCRIPTS_DIR/memory-search.py" --help &>/dev/null; then
    ok "memory-search.py 可正常运行"
else
    warn "memory-search.py 运行检查失败 (可能缺少依赖)"
fi

echo ""

# --- 完成 ---
if [ "$ERRORS" -eq 0 ]; then
    echo "=========================================="
    echo -e "  ${GREEN}安装完成!${NC}"
    echo "=========================================="
else
    echo "=========================================="
    echo -e "  ${YELLOW}安装完成 (有 $ERRORS 个警告)${NC}"
    echo "=========================================="
fi

echo ""
echo "后续步骤:"
echo "  1. 编辑 ~/.claude/memory/MEMORY.md 填入你的偏好"
echo "  2. 编辑 ~/.claude/memory/config.json 配置项目映射"
echo "  3. (可选) pip install fastembed  # 启用向量搜索"
echo "  4. 构建搜索索引: python3 ~/.claude/scripts/memory-search.py --rebuild"
echo ""
echo "卸载: bash $SCRIPT_DIR/install.sh --uninstall"
echo ""
