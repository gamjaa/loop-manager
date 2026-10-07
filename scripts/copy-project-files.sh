#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "사용법: $0 /path/to/project" >&2
    exit 2
fi

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
target="$1"

if [[ ! -d "$target" ]]; then
    echo "대상 디렉터리가 존재하지 않습니다: $target" >&2
    exit 1
fi

target="$(cd -- "$target" && pwd)"
conflicts=()

for path in "$target/.agents" "$target/docs"; do
    [[ -e "$path" || -L "$path" ]] && conflicts+=("$path")
done
if [[ -e "$target/AGENTS.md" || -L "$target/AGENTS.md" ]]; then
    if [[ ! -f "$target/AGENTS.md" || -L "$target/AGENTS.md" ]]; then
        conflicts+=("$target/AGENTS.md (일반 파일이 아님)")
    fi
fi

if (( ${#conflicts[@]} > 0 )); then
    printf '이미 존재하는 파일 또는 폴더: %s\n' "${conflicts[@]}" >&2
    exit 1
fi

if [[ ! -e "$target/AGENTS.md" ]]; then
    (set -o noclobber; cat > "$target/AGENTS.md" <<'GUIDANCE'
# Project instructions

프로젝트 전용 언어·코딩·빌드 규칙은 이 파일에 기록한다.
문서 작성·검토 기준은 [Specification Skill](.agents/skills/specification-docs/SKILL.md)을 따른다.
docs/PRODUCT.md 또는 활성 Requirement 중 하나는 반드시 존재해야 한다.
GUIDANCE
    )
fi
cp -R "$source_root/.agents" "$target/.agents"

mkdir -p \
    "$target/docs/plans" \
    "$target/docs/requirements/archive"

cp "$source_root/docs/PLANS.md" "$target/docs/PLANS.md"
cp "$source_root/docs/FUTURE_IDEAS.md" "$target/docs/FUTURE_IDEAS.md"
cp "$source_root"/docs/*.template.md "$target/docs/"
cp "$source_root/docs/PRODUCT.template.md" "$target/docs/PRODUCT.md"
cp "$source_root/docs/ARCHITECTURE.template.md" "$target/docs/ARCHITECTURE.md"

echo "Loop Manager 프로젝트 파일을 복사했습니다: $target"
