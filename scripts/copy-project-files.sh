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
if [[ "$target" == "$source_root" ]]; then
    echo '복사 원본과 같은 디렉터리는 지정할 수 없습니다.' >&2
    exit 1
fi
mkdir -p \
    "$target/.agents" \
    "$target/docs/plans/archive" \
    "$target/docs/requirements/archive"

for skill in "$source_root/.agents/skills/"*/; do
    [[ -d "$skill" ]] || continue
    rm -rf -- "$target/.agents/skills/$(basename -- "$skill")"
done
cp -Rf "$source_root/.agents/." "$target/.agents/"

cp "$source_root/docs/PLANS.md" "$target/docs/PLANS.md"
printf '# 후속 아이디어\n' > "$target/docs/FUTURE_IDEAS.md"
cp "$source_root"/docs/*.template.md "$target/docs/"
cp "$source_root/docs/PRODUCT.template.md" "$target/docs/PRODUCT.md"
cp "$source_root/docs/ARCHITECTURE.template.md" "$target/docs/ARCHITECTURE.md"

echo "Loop Manager 프로젝트 파일을 복사했습니다: $target"
