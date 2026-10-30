# tools

This directory holds symlinks to external programs installed on this machine, and nothing else. No binaries or installers belong here. Blender, 7-Zip and unrar stay where they are installed.

Create or refresh the links from the project root:

```bash
./scripts/link_tools.sh
```

The links are specific to each machine, so git ignores everything here except this file.

To point a link at a specific installation, set an override:

```bash
BLENDER_BIN="/Applications/Blender 4.3.app/Contents/MacOS/Blender" ./scripts/link_tools.sh
```

The overrides are `BLENDER_BIN`, `SEVEN_ZIP_BIN` and `UNRAR_BIN`.

The Python code that calls these programs lives in `src/desync_archival/external/`.
