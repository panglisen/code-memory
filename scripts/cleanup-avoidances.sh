#!/bin/bash
# cleanup-avoidances.sh - 清理 MEMORY.md 中膨胀的 auto 避坑经验
# 策略: 提取 auto 条目 -> LLM 分批去重合并 -> 重写 MEMORY.md 的避坑经验区块
# 用法: bash ~/.claude/scripts/cleanup-avoidances.sh [--dry-run]

set -euo pipefail

MEMORY_FILE="$HOME/.claude/memory/MEMORY.md"
DRY_RUN=false
BATCH_SIZE=50
SUMMARY_MODEL="haiku"
CLAUDE_TIMEOUT=60

if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    echo "[DRY-RUN] 仅预览，不会修改文件"
fi

if [ ! -f "$MEMORY_FILE" ]; then
    echo "ERROR: $MEMORY_FILE 不存在"
    exit 1
fi

# Step 0: 备份
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${MEMORY_FILE}.bak.${TIMESTAMP}"
cp "$MEMORY_FILE" "$BACKUP_FILE"
echo "[备份] $BACKUP_FILE"

TMPDIR_CLEANUP=$(mktemp -d "${TMPDIR:-/tmp}/cleanup-avoidances.XXXXXX")
trap 'rm -rf "$TMPDIR_CLEANUP"' EXIT

# Step 1: 分离文件为三部分
# - before_section: ## 避坑经验 之前的内容
# - manual_entries: 避坑经验区块中的手动条目（不含 auto 标记）
# - auto_entries: 避坑经验区块中的 auto 条目
# - after_section: ## 避坑经验 之后的内容（下一个 ## 开始）

BEFORE_FILE="$TMPDIR_CLEANUP/before.md"
MANUAL_FILE="$TMPDIR_CLEANUP/manual.md"
AUTO_FILE="$TMPDIR_CLEANUP/auto.txt"
AFTER_FILE="$TMPDIR_CLEANUP/after.md"

python3 - "$MEMORY_FILE" "$BEFORE_FILE" "$MANUAL_FILE" "$AUTO_FILE" "$AFTER_FILE" << 'PYEOF'
import sys
import re

memory_file, before_file, manual_file, auto_file, after_file = sys.argv[1:6]

with open(memory_file, encoding='utf-8') as f:
    content = f.read()

# 找到 ## 避坑经验 的位置
avoidance_match = re.search(r'^## 避坑经验\s*$', content, re.MULTILINE)
if not avoidance_match:
    print("ERROR: 找不到 '## 避坑经验' 区块")
    sys.exit(1)

avoidance_start = avoidance_match.start()

# 找到 ## 避坑经验 之后的下一个 ## 区块
after_match = re.search(r'^## ', content[avoidance_match.end():], re.MULTILINE)
if after_match:
    section_end = avoidance_match.end() + after_match.start()
else:
    section_end = len(content)

before_content = content[:avoidance_start]
avoidance_content = content[avoidance_match.end():section_end]
after_content = content[section_end:]

# 分离手动条目和 auto 条目
manual_lines = []
auto_entries = []

for line in avoidance_content.split('\n'):
    stripped = line.strip()
    if not stripped or stripped == '':
        continue
    if stripped.startswith('- '):
        if '(auto)' in stripped or ', auto)' in stripped:
            # 提取 **...** 中的文本
            m = re.search(r'\*\*(.*?)\*\*', stripped)
            if m:
                auto_entries.append(m.group(1))
            else:
                # 没有 ** 包裹的 auto 条目
                text = re.sub(r'^\- ', '', stripped)
                text = re.sub(r'\(.*?auto\)$', '', text).strip()
                auto_entries.append(text)
        else:
            manual_lines.append(line)

with open(before_file, 'w', encoding='utf-8') as f:
    f.write(before_content)

with open(manual_file, 'w', encoding='utf-8') as f:
    f.write('\n'.join(manual_lines))

with open(auto_file, 'w', encoding='utf-8') as f:
    for entry in auto_entries:
        f.write(entry + '\n')

with open(after_file, 'w', encoding='utf-8') as f:
    f.write(after_content)

print(f"[分离] 手动条目: {len(manual_lines)}, Auto 条目: {len(auto_entries)}")
PYEOF

AUTO_COUNT=$(wc -l < "$AUTO_FILE" | tr -d ' ')
echo "[统计] Auto 条目总数: $AUTO_COUNT"

if [ "$AUTO_COUNT" -eq 0 ]; then
    echo "没有 auto 条目需要清理"
    exit 0
fi

# Step 2: 分批发送给 LLM 合并去重
MERGED_FILE="$TMPDIR_CLEANUP/merged_all.txt"
: > "$MERGED_FILE"

batch_num=0
while IFS= read -r line || [ -n "$line" ]; do
    echo "$line" >> "$TMPDIR_CLEANUP/batch_input.txt"
    batch_num=$((batch_num + 1))

    if [ "$batch_num" -ge "$BATCH_SIZE" ]; then
        echo "[LLM] 处理批次 ($batch_num 条)..."
        BATCH_RESULT=$(cat "$TMPDIR_CLEANUP/batch_input.txt" | perl -e '
            use POSIX ":sys_wait_h";
            $pid = open(my $fh, "-|", @ARGV) or exit 1;
            $SIG{ALRM} = sub { kill("TERM", $pid); sleep(1); kill("KILL", $pid); exit 124; };
            alarm('"$CLAUDE_TIMEOUT"');
            local $/; $out = <$fh>; close $fh;
            print $out;
        ' -- claude -p \
            --model "$SUMMARY_MODEL" \
            --no-session-persistence \
            --system-prompt '你是避坑经验去重合并器。将以下列表去重合并：
- 相同知识点只保留一条最完整的表述
- 删除完全重复或近似重复的条目
- 保持原始措辞风格，不要改写
- 每条一行输出，不加序号和前缀符号
- 直接输出结果，不要加任何解释' 2>/dev/null) || true

        if [ -n "$BATCH_RESULT" ]; then
            printf '%s\n' "$BATCH_RESULT" >> "$MERGED_FILE"
        else
            # LLM 失败，保留原始条目
            cat "$TMPDIR_CLEANUP/batch_input.txt" >> "$MERGED_FILE"
            echo "[WARN] 批次 LLM 失败，保留原始条目" >&2
        fi

        : > "$TMPDIR_CLEANUP/batch_input.txt"
        batch_num=0
    fi
done < "$AUTO_FILE"

# 处理最后一批 (不足 BATCH_SIZE)
if [ -s "$TMPDIR_CLEANUP/batch_input.txt" ]; then
    echo "[LLM] 处理最后一批 ($batch_num 条)..."
    BATCH_RESULT=$(cat "$TMPDIR_CLEANUP/batch_input.txt" | perl -e '
        use POSIX ":sys_wait_h";
        $pid = open(my $fh, "-|", @ARGV) or exit 1;
        $SIG{ALRM} = sub { kill("TERM", $pid); sleep(1); kill("KILL", $pid); exit 124; };
        alarm('"$CLAUDE_TIMEOUT"');
        local $/; $out = <$fh>; close $fh;
        print $out;
    ' -- claude -p \
        --model "$SUMMARY_MODEL" \
        --no-session-persistence \
        --system-prompt '你是避坑经验去重合并器。将以下列表去重合并：
- 相同知识点只保留一条最完整的表述
- 删除完全重复或近似重复的条目
- 保持原始措辞风格，不要改写
- 每条一行输出，不加序号和前缀符号
- 直接输出结果，不要加任何解释' 2>/dev/null) || true

    if [ -n "$BATCH_RESULT" ]; then
        printf '%s\n' "$BATCH_RESULT" >> "$MERGED_FILE"
    else
        cat "$TMPDIR_CLEANUP/batch_input.txt" >> "$MERGED_FILE"
        echo "[WARN] 最后一批 LLM 失败，保留原始条目" >&2
    fi
fi

# Step 3: 最终去重轮（如果合并后仍超过 BATCH_SIZE，做最终合并）
MERGED_COUNT=$(wc -l < "$MERGED_FILE" | tr -d ' ')
echo "[统计] 批次合并后: $MERGED_COUNT 条"

FINAL_FILE="$TMPDIR_CLEANUP/final.txt"

if [ "$MERGED_COUNT" -gt "$BATCH_SIZE" ]; then
    echo "[LLM] 最终去重轮..."
    FINAL_RESULT=$(cat "$MERGED_FILE" | perl -e '
        use POSIX ":sys_wait_h";
        $pid = open(my $fh, "-|", @ARGV) or exit 1;
        $SIG{ALRM} = sub { kill("TERM", $pid); sleep(1); kill("KILL", $pid); exit 124; };
        alarm('"$CLAUDE_TIMEOUT"');
        local $/; $out = <$fh>; close $fh;
        print $out;
    ' -- claude -p \
        --model "$SUMMARY_MODEL" \
        --no-session-persistence \
        --system-prompt '你是避坑经验去重合并器。将以下列表做最终去重合并：
- 相同知识点只保留一条最完整的表述
- 删除完全重复或近似重复的条目
- 保持原始措辞风格，不要改写
- 每条一行输出，不加序号和前缀符号
- 直接输出结果，不要加任何解释' 2>/dev/null) || true

    if [ -n "$FINAL_RESULT" ]; then
        printf '%s\n' "$FINAL_RESULT" > "$FINAL_FILE"
    else
        cp "$MERGED_FILE" "$FINAL_FILE"
        echo "[WARN] 最终去重 LLM 失败，使用批次合并结果" >&2
    fi
else
    cp "$MERGED_FILE" "$FINAL_FILE"
fi

# 清理空行和前缀符号
python3 -c "
import sys
seen = set()
result = []
for line in open(sys.argv[1], encoding='utf-8'):
    line = line.strip()
    if not line:
        continue
    # 去除 LLM 可能添加的前缀符号
    import re
    line = re.sub(r'^[-*•]\s*', '', line)
    line = re.sub(r'^\d+[.)]\s*', '', line)
    line = line.strip()
    if not line:
        continue
    if line not in seen:
        seen.add(line)
        result.append(line)
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    for r in result:
        f.write(r + '\n')
print(f'[去重] 最终精华条目: {len(result)}')
" "$FINAL_FILE"

FINAL_COUNT=$(wc -l < "$FINAL_FILE" | tr -d ' ')
echo "[统计] 最终精华条目: $FINAL_COUNT (从 $AUTO_COUNT 合并)"

# Step 4: 重写 MEMORY.md
if [ "$DRY_RUN" = "true" ]; then
    echo ""
    echo "=== DRY-RUN: 合并后的精华条目 ==="
    cat "$FINAL_FILE"
    echo ""
    echo "=== DRY-RUN: 不会修改 $MEMORY_FILE ==="
    echo "[统计] $AUTO_COUNT 条 auto -> $FINAL_COUNT 条精华"
    exit 0
fi

DATE=$(date +%Y-%m-%d)

# 组装新的 MEMORY.md
{
    cat "$BEFORE_FILE"
    echo "## 避坑经验"
    echo ""
    # 手动条目原样保留
    if [ -s "$MANUAL_FILE" ]; then
        cat "$MANUAL_FILE"
        echo ""
    fi
    # auto 精华条目
    while IFS= read -r entry; do
        [ -z "$entry" ] && continue
        echo "- **${entry}** ($DATE, auto)"
    done < "$FINAL_FILE"
    echo ""
    cat "$AFTER_FILE"
} > "$MEMORY_FILE"

NEW_LINE_COUNT=$(wc -l < "$MEMORY_FILE" | tr -d ' ')
echo ""
echo "=== 清理完成 ==="
echo "备份: $BACKUP_FILE"
echo "原始 auto 条目: $AUTO_COUNT"
echo "清理后: $NEW_LINE_COUNT 行, $FINAL_COUNT 条精华 auto"
echo "手动条目: 原样保留"
