#!/bin/bash
# weekly-consolidate.sh - 每周整理记忆系统
# 建议通过 cron 每周日运行: 0 10 * * 0 ~/.claude/scripts/weekly-consolidate.sh
# 或手动运行
#
# 改进: 增加自动知识提炼步骤
# - 分析本周高频编辑文件，自动生成项目 facts
# - 从 sessions 摘要中提取重复模式
# - 刷新 BM25 搜索索引

set -e

MEMORY_DIR="$HOME/.claude/memory"
DAILY_DIR="$MEMORY_DIR/daily"
SESSIONS_DIR="$MEMORY_DIR/sessions"
MEMORY_FILE="$MEMORY_DIR/MEMORY.md"
AREAS_DIR="$MEMORY_DIR/areas"

# 配置
SESSIONS_RETAIN_DAYS=30
DAILY_RETAIN_DAYS=90

echo "=========================================="
echo "  记忆系统周整理 - $(date +%Y-%m-%d)"
echo "=========================================="
echo ""

# 1. 统计本周的每日笔记
echo "## 1. 本周活动统计"
echo ""

WEEK_AGO=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d "7 days ago" +%Y-%m-%d)
echo "统计范围: $WEEK_AGO 至今"
echo ""

DAILY_COUNT=$(find "$DAILY_DIR" -name "*.md" -mtime -7 2>/dev/null | wc -l | tr -d ' ')
echo "- 每日笔记数: $DAILY_COUNT"
echo ""

# 2. 显示本周编辑的项目
echo "## 2. 本周活跃项目"
echo ""

if [ -d "$DAILY_DIR" ]; then
    grep -h "项目:" "$DAILY_DIR"/*.md 2>/dev/null | sort | uniq -c | sort -rn | head -10 || echo "暂无记录"
fi
echo ""

# 3. 提取高频编辑文件 (新增: 自动知识提炼)
echo "## 3. 本周高频编辑文件分析"
echo ""

if [ -d "$DAILY_DIR" ]; then
    # 从每日笔记中提取所有被编辑的文件名
    HIGH_FREQ_FILES=$(grep -h '`[^`]*\.[a-zA-Z]*`' "$DAILY_DIR"/*.md 2>/dev/null \
        | grep -o '`[^`]*\.[a-zA-Z]*`' \
        | tr -d '`' \
        | sort | uniq -c | sort -rn | head -15)

    if [ -n "$HIGH_FREQ_FILES" ]; then
        echo "被编辑次数最多的文件:"
        echo "$HIGH_FREQ_FILES" | while read -r count filename; do
            echo "  - $filename: ${count}次"
        done

        # 提取编辑 >= 3 次的文件，这些是"热点文件"
        HOT_FILES=$(echo "$HIGH_FREQ_FILES" | awk '$1 >= 3 {print $2}')
        HOT_COUNT=$(echo "$HOT_FILES" | grep -c . 2>/dev/null || echo "0")

        if [ "$HOT_COUNT" -gt 0 ]; then
            echo ""
            echo "热点文件 (编辑 >= 3 次): $HOT_COUNT 个"
            echo "$HOT_FILES" | while read -r filename; do
                echo "  -> $filename"
            done
        fi
    else
        echo "本周无编辑记录"
    fi
fi
echo ""

# 4. Session 摘要模式提取 (新增)
echo "## 4. 本周会话主题分析"
echo ""

if [ -d "$SESSIONS_DIR" ]; then
    # 从本周修改的 session 摘要中提取主题
    RECENT_SESSIONS=$(find "$SESSIONS_DIR" -name "*.md" -mtime -7 2>/dev/null || true)
    if [ -n "$RECENT_SESSIONS" ]; then
        SESSION_COUNT=$(echo "$RECENT_SESSIONS" | wc -l | tr -d ' ')
        echo "本周会话数: $SESSION_COUNT"
        echo ""

        # 如果是摘要格式，提取 **主题** 行
        echo "会话主题:"
        echo "$RECENT_SESSIONS" | while read -r f; do
            [ -f "$f" ] || continue
            # 尝试提取摘要中的主题
            topic=$(grep -m1 '\*\*主题\*\*' "$f" 2>/dev/null | sed 's/.*\*\*主题\*\*[：:]\s*//')
            if [ -n "$topic" ]; then
                echo "  - $topic"
            else
                # 从标题中提取
                project=$(grep -m1 '^\- \*\*项目\*\*:' "$f" 2>/dev/null | sed 's/.*: //')
                echo "  - [${project:-未知项目}] $(basename "$f" .md)"
            fi
        done
    else
        echo "本周无会话记录"
    fi
fi
echo ""

# 5. 检查知识图谱状态
echo "## 5. 知识图谱状态"
echo ""

for area in projects patterns tools; do
    if [ -d "$AREAS_DIR/$area" ]; then
        COUNT=$(find "$AREAS_DIR/$area" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
        echo "- $area: $COUNT 个实体"

        # 列出各实体的事实数量
        for entity_dir in "$AREAS_DIR/$area"/*/; do
            if [ -d "$entity_dir" ]; then
                entity_name=$(basename "$entity_dir")
                facts_file="$entity_dir/facts.json"
                if [ -f "$facts_file" ] && command -v jq &> /dev/null; then
                    fact_count=$(jq '.facts | length' "$facts_file" 2>/dev/null || echo "0")
                    active_count=$(jq '[.facts[] | select(.status == "active")] | length' "$facts_file" 2>/dev/null || echo "0")
                    echo "  - $entity_name: $fact_count 条事实 ($active_count 条活跃)"
                fi
            fi
        done
    else
        echo "- $area: 0 个实体"
    fi
done
echo ""

# 6. 磁盘空间统计
echo "## 6. 磁盘空间统计"
echo ""

if [ -d "$SESSIONS_DIR" ]; then
    SESSION_COUNT=$(find "$SESSIONS_DIR" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
    SESSION_SIZE=$(du -sh "$SESSIONS_DIR" 2>/dev/null | cut -f1)
    echo "- 会话文件: $SESSION_COUNT 个, 共 $SESSION_SIZE"

    # 检查是否有未迁移的大文件
    LARGE_SESSIONS=$(find "$SESSIONS_DIR" -name "*.md" -size +100k 2>/dev/null | wc -l | tr -d ' ')
    if [ "$LARGE_SESSIONS" -gt 0 ]; then
        echo "  [警告] 发现 $LARGE_SESSIONS 个大于 100KB 的会话文件 (可能未迁移为摘要)"
        echo "  运行: ~/.claude/scripts/migrate-sessions.sh --dry-run 查看详情"
    fi
fi

if [ -d "$DAILY_DIR" ]; then
    DAILY_SIZE=$(du -sh "$DAILY_DIR" 2>/dev/null | cut -f1)
    echo "- 每日笔记: $DAILY_SIZE"
fi

# 搜索索引大小
if [ -f "$MEMORY_DIR/.search-index.db" ]; then
    INDEX_SIZE=$(du -sh "$MEMORY_DIR/.search-index.db" 2>/dev/null | cut -f1)
    echo "- 搜索索引: $INDEX_SIZE"
fi

TOTAL_SIZE=$(du -sh "$MEMORY_DIR" 2>/dev/null | cut -f1)
echo "- 记忆系统总计: $TOTAL_SIZE"
echo ""

# 7. 刷新搜索索引 (新增)
echo "## 7. 刷新搜索索引"
echo ""

if [ -f "$HOME/.claude/scripts/memory-search.py" ]; then
    python3 "$HOME/.claude/scripts/memory-search.py" --rebuild 2>&1
    echo ""
else
    echo "搜索索引刷新跳过 (python3 或 memory-search.py 不可用)"
fi
echo ""

# 8. 自动清理过期会话文件
echo "## 8. 自动清理"
echo ""

if [ -d "$SESSIONS_DIR" ]; then
    OLD_SESSIONS=$(find "$SESSIONS_DIR" -name "*.md" -mtime +$SESSIONS_RETAIN_DAYS 2>/dev/null || true)
    if [ -z "$OLD_SESSIONS" ]; then
        OLD_COUNT=0
    else
        OLD_COUNT=$(echo "$OLD_SESSIONS" | wc -l | tr -d ' ')
    fi

    if [ "$OLD_COUNT" -gt 0 ] && [ -n "$OLD_SESSIONS" ]; then
        echo "- 发现 $OLD_COUNT 个超过 ${SESSIONS_RETAIN_DAYS} 天的会话文件"

        # 执行清理 (添加 --dry-run 参数可预览)
        if [ "$1" = "--dry-run" ]; then
            echo "  [预览模式] 以下文件将被删除:"
            echo "$OLD_SESSIONS" | while read -r f; do
                echo "    - $(basename "$f")"
            done
        else
            echo "$OLD_SESSIONS" | while read -r f; do
                if [ -f "$f" ]; then
                    rm "$f"
                    echo "  - 已删除: $(basename "$f")"
                fi
            done
            echo "  清理完成"
        fi
    else
        echo "- 无需清理 (所有会话文件均在 ${SESSIONS_RETAIN_DAYS} 天内)"
    fi
fi

# 清理过期每日笔记
if [ -d "$DAILY_DIR" ]; then
    OLD_DAILIES=$(find "$DAILY_DIR" -name "*.md" -mtime +$DAILY_RETAIN_DAYS 2>/dev/null || true)
    if [ -z "$OLD_DAILIES" ]; then
        OLD_DAILY_COUNT=0
    else
        OLD_DAILY_COUNT=$(echo "$OLD_DAILIES" | wc -l | tr -d ' ')
    fi

    if [ "$OLD_DAILY_COUNT" -gt 0 ] && [ -n "$OLD_DAILIES" ]; then
        echo "- 发现 $OLD_DAILY_COUNT 个超过 ${DAILY_RETAIN_DAYS} 天的每日笔记"

        if [ "$1" = "--dry-run" ]; then
            echo "  [预览模式] 以下文件将被删除:"
            echo "$OLD_DAILIES" | while read -r f; do
                echo "    - $(basename "$f")"
            done
        else
            echo "$OLD_DAILIES" | while read -r f; do
                if [ -f "$f" ]; then
                    rm "$f"
                    echo "  - 已删除: $(basename "$f")"
                fi
            done
        fi
    else
        echo "- 每日笔记无需清理 (均在 ${DAILY_RETAIN_DAYS} 天内)"
    fi
fi

# 清理 sessions-backup (迁移后 7 天可删除)
if [ -d "$MEMORY_DIR/sessions-backup" ]; then
    BACKUP_AGE=$(find "$MEMORY_DIR/sessions-backup" -maxdepth 0 -mtime +7 2>/dev/null | wc -l | tr -d ' ')
    if [ "$BACKUP_AGE" -gt 0 ]; then
        BACKUP_SIZE=$(du -sh "$MEMORY_DIR/sessions-backup" 2>/dev/null | cut -f1)
        if [ "$1" = "--dry-run" ]; then
            echo "- [预览] sessions-backup ($BACKUP_SIZE) 可删除 (已超过 7 天)"
        else
            rm -rf "$MEMORY_DIR/sessions-backup"
            echo "- 已清理 sessions-backup ($BACKUP_SIZE)"
        fi
    fi
fi
echo ""

# 9. 知识提炼建议 (新增: 基于本周数据生成具体建议)
echo "## 9. 知识提炼建议"
echo ""

# 9a. 检查未摘要的会话
UNSUMMARIZED=$(find "$SESSIONS_DIR" -name "*.md" -size +50k 2>/dev/null | wc -l | tr -d ' ')
if [ "$UNSUMMARIZED" -gt 0 ]; then
    echo "- [$UNSUMMARIZED 个会话] 未生成摘要，建议运行:"
    echo "  ~/.claude/scripts/migrate-sessions.sh"
    echo ""
fi

# 9b. 检查长期未更新的 summary.md
echo "- 检查项目 summary.md 更新状态:"
for proj_dir in "$AREAS_DIR/projects"/*/; do
    [ -d "$proj_dir" ] || continue
    proj_name=$(basename "$proj_dir")
    summary_file="$proj_dir/summary.md"
    if [ -f "$summary_file" ]; then
        last_mod=$(stat -f "%Sm" -t "%Y-%m-%d" "$summary_file" 2>/dev/null || stat -c "%y" "$summary_file" 2>/dev/null | cut -d' ' -f1)
        echo "  - $proj_name: 最后更新 $last_mod"
    else
        echo "  - $proj_name: 无 summary.md"
    fi
done
echo ""

# 9c. 检查 MEMORY.md 最后更新
echo "## 10. 自动事实提取"
echo ""

EXTRACT_SCRIPT="$HOME/.claude/scripts/auto-extract-facts.py"
if command -v python3 &> /dev/null && [ -f "$EXTRACT_SCRIPT" ]; then
    if [ "$1" = "--dry-run" ]; then
        echo "运行自动事实提取 (预览模式)..."
        python3 "$EXTRACT_SCRIPT" --dry-run 2>&1
    else
        echo "运行自动事实提取..."
        python3 "$EXTRACT_SCRIPT" 2>&1
    fi
else
    echo "自动事实提取跳过 (python3 或 auto-extract-facts.py 不可用)"
fi
echo ""

echo "## 11. MEMORY.md 状态"
echo ""

if [ -f "$MEMORY_FILE" ]; then
    LAST_MOD=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$MEMORY_FILE" 2>/dev/null || stat -c "%y" "$MEMORY_FILE" 2>/dev/null | cut -d'.' -f1)
    echo "- 最后更新: $LAST_MOD"
    echo "- 文件大小: $(wc -c < "$MEMORY_FILE" | tr -d ' ') 字节"

    # 检查避坑经验和最佳实践数量
    if command -v grep &> /dev/null; then
        AVOID_COUNT=$(grep -c '^\- \*\*' "$MEMORY_FILE" 2>/dev/null || echo "0")
        echo "- 记录条目: 约 $AVOID_COUNT 条"
    fi
else
    echo "- 状态: 文件不存在"
fi
echo ""

echo "=========================================="
echo "  整理完成"
echo "=========================================="
echo ""
echo "建议手动操作:"
echo "1. 查看上方高频编辑文件，将重复出现的模式添加到 patterns/"
echo "2. 将有价值的避坑经验添加到 MEMORY.md"
echo "3. 运行 /memory-summarize 提炼本周知识"
