# Fix 1: builder_rules.md - change step 4 from MANDATORY to suggested
path = r"E:\my-code\workflow\codepilot\prompts\builder_rules.md"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    "=== 第四步：强制记忆写入（实现后必须做）===",
    "=== 第四步：建议记忆写入（有价值时再做）==="
)
text = text.replace(
    "实现完成后、输出 Summary 之前，你必须将关键发现写入记忆：",
    "如果你发现了非显而易见的模式、踩坑经验、或后续任务需要知道的关键上下文，可以写入记忆："
)
text = text.replace(
    "- 第四步的强制记忆写入不是建议，是强制要求。不写记忆的 Builder 产出会被标记为不完整。",
    "- 第四步的记忆写入是有价值时才做的建议，不是强制要求。不要为了凑数写无意义的垃圾记忆。"
)
text = text.replace(
    "6. 实现完成后必须至少写一条 note 记录关键发现。",
    "6. 如果发现了非显而易见的模式、踩坑经验或重要上下文，建议写一条 note 以便后续任务受益。不要写重复或琐碎的内容。"
)

with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("builder_rules.md OK")

# Fix 2: builder_rules.en.md
path = r"E:\my-code\workflow\codepilot\prompts\builder_rules.en.md"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    "=== STEP 4: MANDATORY MEMORY WRITE (do this AFTER implementing) ===",
    "=== STEP 4: SUGGESTED MEMORY WRITE (only when valuable) ==="
)
text = text.replace(
    "After implementation and before outputting your Summary, you MUST write\nkey findings to memory so future tasks benefit:",
    "If you discovered non-obvious patterns, pitfalls, or important context that\nfuture tasks will need, you SHOULD write key findings to memory:"
)
text = text.replace(
    "- STEP 4 is NOT optional. Write notes after discovering patterns.",
    "- STEP 4 is a suggestion, not a requirement. Only write notes when you have\ngenuinely useful findings. Do not create junk entries."
)
text = text.replace(
    "6. AFTER implementation, write at least one note with key findings so the\n   next task does not waste time rediscovering them.",
    "6. If you discovered non-obvious patterns, pitfalls, or important context,\n   write a note so the next task does not waste time. Do not write trivial or\n   repetitive content."
)

with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("builder_rules.en.md OK")

# Fix 3: run_builtin_prompts.py - change step 4 label
path = r"E:\my-code\workflow\codepilot\commands\run_builtin_prompts.py"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    "# STEP 4 - MANDATORY: Write findings after implementing",
    "# STEP 4 - SUGGESTED: Write findings if valuable"
)
text = text.replace(
    "Step 4 (memory write) are MANDATORY.",
    "Step 4 (memory write) is suggested."
)
text = text.replace(
    "Skipping them will cause review failure.",
    "Skipping step 1 will cause review failure; step 4 is optional."
)

with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("run_builtin_prompts.py OK")

# Fix 4: profile.py - change write instruction from mandatory to suggested
path = r"E:\my-code\workflow\codepilot\opencode\profile.py"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

# Chinese: change point 4
text = text.replace(
    "\"4. 完成任务后，必须将关键发现写入 note_add 或 wiki_add，以便后续任务受益\\\\n\"",
    "\"4. 如果发现了有价值的模式或踩坑经验，建议写入 note_add 或 wiki_add，以便后续任务受益\\\\n\""
)
# English: change point 4
text = text.replace(
    "\"4. AFTER completing tasks, write key findings to note_add or wiki_add for future tasks\\\\n\"",
    "\"4. When you discover valuable patterns or pitfalls, write key findings to note_add or wiki_add for future tasks\\\\n\""
)

with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("profile.py OK")

# Fix 5: task_recon.md - change write from mandatory to suggested
path = r"E:\my-code\workflow\codepilot\prompts\task_recon.md"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    "[After inspection - MANDATORY memory write]",
    "[After inspection - SUGGESTED memory write]"
)
text = text.replace(
    "After producing your recon findings, write key observations to memory:",
    "If your findings contain non-obvious patterns or important context, write key observations to memory:"
)
text = text.replace(
    "This ensures the planner and later tasks benefit from your inspection work.",
    "This helps the planner and later tasks benefit from your inspection work, but only write if there is genuine value."
)

with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("task_recon.md OK")

# Fix 6: task_recon.en.md
path = r"E:\my-code\workflow\codepilot\prompts\task_recon.en.md"
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    "[After inspection - MANDATORY memory write]",
    "[After inspection - SUGGESTED memory write]"
)
text = text.replace(
    "After producing your recon findings, write key observations to memory:",
    "If your findings contain non-obvious patterns or important context, write key observations to memory:"
)
text = text.replace(
    "This ensures the planner and later tasks benefit from your inspection work.",
    "This helps the planner and later tasks benefit from your inspection work, but only write if there is genuine value."
)

with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("task_recon.en.md OK")

print("ALL DONE")
