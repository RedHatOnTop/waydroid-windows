#!/bin/bash
# Rebuilds wdtask.dex from WdTask.java with the JDK's compiler and Google's D8. Only needed after
# changing WdTask.java: the repository carries a built wdtask.dex, and wlproxy pushes it into the
# container itself whenever the copy there differs.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
r8_version=9.5.23
r8=${R8_JAR:-$HOME/.cache/waydroid-windows/r8-$r8_version.jar}
if [[ ! -f $r8 ]]; then
  mkdir -p "$(dirname "$r8")"
  curl -fL -o "$r8" "https://dl.google.com/android/maven2/com/android/tools/r8/$r8_version/r8-$r8_version.jar"
fi
build=$(mktemp -d)
trap 'rm -r "$build"' EXIT
java "$here/Compile.java" -d "$build/classes" -source 17 -target 17 -nowarn -Xlint:-options "$here/WdTask.java"
java -cp "$r8" com.android.tools.r8.D8 --release --min-api 30 --output "$build/wdtask.zip" "$build/classes/WdTask.class"
python3 -I -c "import zipfile, sys; open(sys.argv[2], 'wb').write(zipfile.ZipFile(sys.argv[1]).read('classes.dex'))" \
  "$build/wdtask.zip" "$here/wdtask.dex"
echo "built $here/wdtask.dex"
