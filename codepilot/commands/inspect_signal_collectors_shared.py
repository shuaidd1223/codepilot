"""Shared scan metadata for inspect signal collectors."""

from __future__ import annotations

import re
from pathlib import Path

SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".md"}
CODE_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".rb",
    ".swift",
    ".scala",
    ".sh",
    ".ps1",
}
LANG_BY_EXT = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".c": "C/C++",
    ".cc": "C/C++",
    ".cpp": "C/C++",
    ".h": "C/C++",
    ".hpp": "C/C++",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".ps1": "PowerShell",
}
COMMENT_MARKERS_BY_EXT = {
    ".py": ("#",),
    ".js": ("//", "/*", "*"),
    ".jsx": ("//", "/*", "*"),
    ".ts": ("//", "/*", "*"),
    ".tsx": ("//", "/*", "*"),
    ".go": ("//", "/*", "*"),
    ".rs": ("//", "/*", "*"),
    ".java": ("//", "/*", "*"),
    ".kt": ("//", "/*", "*"),
    ".c": ("//", "/*", "*"),
    ".cc": ("//", "/*", "*"),
    ".cpp": ("//", "/*", "*"),
    ".h": ("//", "/*", "*"),
    ".hpp": ("//", "/*", "*"),
    ".cs": ("//", "/*", "*"),
    ".php": ("//", "/*", "*", "#"),
    ".rb": ("#",),
    ".swift": ("//", "/*", "*"),
    ".scala": ("//", "/*", "*"),
    ".sh": ("#",),
    ".ps1": ("#",),
}
COMPLEXITY_RE_BY_EXT = {
    ".py": re.compile(r"\b(if|elif|for|while|except|with|assert|and|or|case)\b"),
    ".js": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".jsx": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".ts": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".tsx": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".go": re.compile(r"\b(if|for|case|select|&&|\|\|)\b"),
    ".rs": re.compile(r"\b(if|for|while|match|&&|\|\|)\b"),
    ".java": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".kt": re.compile(r"\b(if|for|while|when|catch|&&|\|\||\?)\b"),
    ".c": re.compile(r"\b(if|else\s+if|for|while|case|&&|\|\||\?)\b"),
    ".cc": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".cpp": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".h": re.compile(r"\b(if|else\s+if|for|while|case|&&|\|\||\?)\b"),
    ".hpp": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".cs": re.compile(r"\b(if|else\s+if|for|while|case|catch|&&|\|\||\?)\b"),
    ".php": re.compile(r"\b(if|elseif|for|foreach|while|case|catch|&&|\|\||\?)\b"),
    ".rb": re.compile(r"\b(if|elsif|unless|for|while|rescue|case|&&|\|\|)\b"),
    ".swift": re.compile(r"\b(if|for|while|case|catch|guard|&&|\|\||\?)\b"),
    ".scala": re.compile(r"\b(if|for|while|case|catch|&&|\|\|)\b"),
    ".sh": re.compile(r"\b(if|for|while|case|elif|&&|\|\|)\b"),
    ".ps1": re.compile(r"\b(if|elseif|foreach|for|while|switch|catch|-and|-or)\b", re.IGNORECASE),
}
SKIP_SCAN_PATH_PARTS = {"node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__"}


def should_skip_scan_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return bool(parts & SKIP_SCAN_PATH_PARTS)
