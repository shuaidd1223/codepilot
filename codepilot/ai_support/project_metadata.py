"""Shared project metadata for AI-facing manifest and guides."""

from __future__ import annotations

PROJECT_AUTHOR_NAME = "帅呆呆"
PROJECT_AUTHOR_EMAIL = "2264505396@qq.com"
PROJECT_REPOSITORY = "https://gitee.com/shuai_dd/CodePilot"
PROJECT_LICENSE = "MIT"


def project_metadata() -> dict[str, object]:
    return {
        "author": {"name": PROJECT_AUTHOR_NAME, "email": PROJECT_AUTHOR_EMAIL},
        "repository": PROJECT_REPOSITORY,
        "license": PROJECT_LICENSE,
    }


def project_metadata_markdown(*, language: str) -> str:
    if language == "zh-CN":
        return (
            f"**作者：** {PROJECT_AUTHOR_NAME} <{PROJECT_AUTHOR_EMAIL}> | "
            f"**仓库：** {PROJECT_REPOSITORY} | **许可：** {PROJECT_LICENSE}"
        )
    return (
        f"**Author:** {PROJECT_AUTHOR_NAME} <{PROJECT_AUTHOR_EMAIL}> | "
        f"**Repository:** {PROJECT_REPOSITORY} | **License:** {PROJECT_LICENSE}"
    )
