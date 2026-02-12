# extract-conversation.jq
# 从 Claude Code JSONL transcript 中提取用户和助手的文本消息
# 用法: jq -r -f extract-conversation.jq transcript.jsonl

select(.type == "user" or .type == "assistant") |
if .type == "user" then
    "用户: " + (
        if .message.content then
            if (.message.content | type) == "string" then
                .message.content
            elif (.message.content | type) == "array" then
                [.message.content[] | select(.type == "text") | .text] | join("\n")
            else
                ""
            end
        else
            ""
        end
    )
elif .type == "assistant" then
    "助手: " + (
        if .message.content then
            if (.message.content | type) == "array" then
                [.message.content[] | select(.type == "text") | .text] | join("\n")
            else
                (.message.content | tostring)
            end
        else
            ""
        end
    )
else
    empty
end |
select(length > 5)
