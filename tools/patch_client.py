#!/usr/bin/env python3
"""Turn a RustDesk source tree into a controlled-only portable client.

Runs inside GitHub Actions, right after checkout, before any build step.
All inputs come from environment variables (never interpolated into shell).

  RD_SERVER        ID/relay server host, without port  (required)
  RD_PUB_KEY       contents of id_ed25519.pub          (required)
  RD_APP_NAME      display name / window title         (optional, default 西瓜远程)
  RD_PRESET_PASSWORD  fixed permanent password        (optional, empty = the
                      controlled side sets its own password in the UI, which
                      is kept across restarts)
  RD_AUTOSTART_NAME  HKCU Run value name              (optional, ASCII)
  RD_HINT          one-line hint shown on the UI       (optional)
  RD_SHOW_PASSWORD "1" to keep the password board     (optional, default 1)
  RD_SRC           source tree root                   (optional, default .)
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("RD_SRC", ".")).resolve()
SERVER = os.environ.get("RD_SERVER", "").strip()
PUBKEY = os.environ.get("RD_PUB_KEY", "").strip()
APP_NAME = os.environ.get("RD_APP_NAME", "西瓜远程").strip() or "西瓜远程"
# ASCII folder: config, ID and the user-chosen password live beside the exe.
DATA_DIR_NAME = "lollipop-data"
AUTOSTART_NAME = os.environ.get("RD_AUTOSTART_NAME", "LollipopAssist").strip() or "LollipopAssist"
PRESET_PASSWORD = os.environ.get("RD_PRESET_PASSWORD", "").strip()
HINT = os.environ.get(
    "RD_HINT", "西瓜远程 · 本设备仅作为被控端；可设置自己的连接密码并开启开机自启，重启后仍然有效"
)
SHOW_PW = os.environ.get("RD_SHOW_PASSWORD", "1") == "1"
cleanup_note = f"结束时请删除本程序及同目录的「{DATA_DIR_NAME}」文件夹"
if cleanup_note not in HINT:
    HINT = f"{HINT}；{cleanup_note}"

if not SERVER or not PUBKEY:
    sys.exit("RD_SERVER and RD_PUB_KEY are required")
for name, value in (
    ("RD_SERVER", SERVER),
    ("RD_PUB_KEY", PUBKEY),
    ("RD_APP_NAME", APP_NAME),
    ("RD_AUTOSTART_NAME", AUTOSTART_NAME),
    ("RD_PRESET_PASSWORD", PRESET_PASSWORD),
):
    if any(c in value for c in '"\\\r\n'):
        sys.exit(f"{name} contains an illegal character")
if not re.fullmatch(r"[A-Za-z0-9_.-]+", AUTOSTART_NAME):
    sys.exit("RD_AUTOSTART_NAME must be ASCII letters/digits/_/./- only")
if PRESET_PASSWORD and len(PRESET_PASSWORD) > 64:
    sys.exit("RD_PRESET_PASSWORD is too long (max 64)")


def read(rel):
    path = ROOT / rel
    if not path.is_file():
        sys.exit(f"missing source file: {path}")
    return path.read_text(encoding="utf-8")


def write(rel, content):
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"[patched] {rel}")


def sub_once(text, pattern, replacement, label, flags=0):
    new, count = re.subn(pattern, lambda _m: replacement, text, count=1, flags=flags)
    if count != 1:
        sys.exit(f"patch anchor not found: {label}")
    return new


# ---------------------------------------------------------------------------
# 1. Bake the relay server + public key into the binary as compile-time consts.
#    Config::path() is redirected to a persistent folder next to the exe so
#    the ID and a user-chosen permanent password survive restarts.
# ---------------------------------------------------------------------------
rel = "libs/hbb_common/src/config.rs"
s = read(rel)
s = sub_once(
    s,
    r"pub const RENDEZVOUS_SERVERS: &\[&str\] = &\[[^\]]*\];",
    f'pub const RENDEZVOUS_SERVERS: &[&str] = &["{SERVER}"];',
    "RENDEZVOUS_SERVERS",
)
s = sub_once(
    s,
    r'pub const RS_PUB_KEY: &str = "[^"]*";',
    f'pub const RS_PUB_KEY: &str = "{PUBKEY}";',
    "RS_PUB_KEY",
)
# APP_NAME drives the window title, tray label and translation substitution.
s = sub_once(
    s,
    r'pub static ref APP_NAME: RwLock<String> = RwLock::new\("[^"]*"\.to_owned\(\)\);',
    f'pub static ref APP_NAME: RwLock<String> = RwLock::new("{APP_NAME}".to_owned());',
    "APP_NAME",
)
# Config::file_() -> Config::path(), so this single redirect covers the ID,
# the permanent password hash, peers, logs and every other config file.
s = sub_once(
    s,
    r"    pub fn path<P: AsRef<Path>>\(p: P\) -> PathBuf \{\n"
    r"        #\[cfg\(any\(target_os = \"android\", target_os = \"ios\"\)\)\]",
    """    pub fn path<P: AsRef<Path>>(p: P) -> PathBuf {
        // Custom controlled-only client: keep config, ID, password and logs
        // in a persistent folder beside the single-file exe, so a password
        // chosen by the controlled side survives restarts. The portable
        // packer exports RUSTDESK_DATA_DIR; every child process inherits it.
        if let Ok(dir) = std::env::var("RUSTDESK_DATA_DIR") {
            if !dir.is_empty() {
                let base = PathBuf::from(dir).join("config");
                if fs::create_dir_all(&base).is_ok() {
                    let mut path = base;
                    path.push(p.as_ref());
                    return path;
                }
                // Read-only location (locked-down share, read-only media):
                // fall back to the normal per-user path below rather than
                // silently losing the password.
            }
        }
        #[cfg(any(target_os = "android", target_os = "ios"))]""",
    "Config::path() redirect",
)
write(rel, s)


# ---------------------------------------------------------------------------
# 2. Compiled-in policy: incoming only, no install/settings/ab/account, and
#    lock the relay server / public key in OVERWRITE_SETTINGS so no config
#    file (including a stale one in the data folder) can override them.
#    Also implements the auto-start switch by writing HKCU\\...\\Run.
# ---------------------------------------------------------------------------
rel = "src/common.rs"
s = read(rel)
s = sub_once(
    s,
    r"pub fn load_custom_client\(\) \{\n",
    "pub fn load_custom_client() {\n"
    "    apply_builtin_hard_settings();\n",
    "load_custom_client entry",
)
if "fn apply_builtin_hard_settings" not in s:
    s += """
/// Compiled-in policy for the controlled-only client.
/// HARD_SETTINGS supplies built-in defaults; OVERWRITE_SETTINGS has the
/// highest priority in Config::get_option() and also makes Config::set_option()
/// refuse changes, so the relay server and public key cannot be overridden.
fn apply_builtin_hard_settings() {
    {
        let mut settings = config::HARD_SETTINGS.write().unwrap();
        settings.insert("conn-type".to_owned(), "incoming".to_owned());
        settings.insert("disable-installation".to_owned(), "Y".to_owned());
        settings.insert("disable-settings".to_owned(), "Y".to_owned());
        settings.insert("disable-ab".to_owned(), "Y".to_owned());
        settings.insert("disable-account".to_owned(), "Y".to_owned());
    }
    let mut locked = config::OVERWRITE_SETTINGS.write().unwrap();
    locked.insert("custom-rendezvous-server".to_owned(), BUILTIN_SERVER.to_owned());
    locked.insert("relay-server".to_owned(), BUILTIN_SERVER.to_owned());
    locked.insert("key".to_owned(), BUILTIN_PUB_KEY.to_owned());
}

/// Toggle "start on boot" for this single-file client.
/// The packer exports RUSTDESK_LAUNCHER_EXE = the path of the exe the user
/// double-clicked (NOT the extracted payload, which disappears on exit).
/// CREATE_NO_WINDOW (0x08000000) keeps reg.exe from flashing a console.
pub fn sync_autostart(value: &str) {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        let enabled = value == "Y" || value.eq_ignore_ascii_case("true") || value == "1";
        const KEY: &str = r"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run";
        let mut cmd = std::process::Command::new("reg");
        if enabled {
            let Ok(launcher) = std::env::var("RUSTDESK_LAUNCHER_EXE") else {
                return;
            };
            if launcher.is_empty() {
                return;
            }
            let data = format!("\\"{}\\"", launcher);
            cmd.args([
                "add",
                KEY,
                "/v",
                AUTOSTART_NAME,
                "/t",
                "REG_SZ",
                "/d",
                data.as_str(),
                "/f",
            ]);
        } else {
            cmd.args(["delete", KEY, "/v", AUTOSTART_NAME, "/f"]);
        }
        let _ = cmd.creation_flags(0x08000000).status();
    }
    #[cfg(not(target_os = "windows"))]
    let _ = value;
}

/// Called once from flutter_ffi::initialize(). Idempotent.
/// Re-applying the registry entry on every launch keeps it valid even if the
/// user moves the exe to another folder.
pub fn apply_builtin_policy() {
    apply_builtin_hard_settings();
    let enabled = config::Config::get_option("ephemeral-autostart");
    sync_autostart(&enabled);
    if !EPHEMERAL_PASSWORD.is_empty() && !Config::has_permanent_password() {
        if !Config::set_permanent_password(EPHEMERAL_PASSWORD) {
            log::error!("failed to set baked-in permanent password");
        }
    }
}
"""
    s += (
        f'\nconst BUILTIN_SERVER: &str = "{SERVER}";\n'
        f'const BUILTIN_PUB_KEY: &str = "{PUBKEY}";\n'
        f'const AUTOSTART_NAME: &str = "{AUTOSTART_NAME}";\n'
        '\n/// Empty = the controlled side chooses its own password in the UI.\n'
        f'const EPHEMERAL_PASSWORD: &str = "{PRESET_PASSWORD}";\n'
    )
write(rel, s)


# ---------------------------------------------------------------------------
# 3. Flutter entry points: policy + auto-start sync on startup, and a hook in
#    main_set_option() so the UI switch takes effect immediately. No bridge
#    regeneration is needed: only function bodies are edited.
# ---------------------------------------------------------------------------
rel = "src/flutter_ffi.rs"
s = read(rel)
s = sub_once(
    s,
    r"pub fn main_set_option\(key: String, value: String\) -> hbb_common::anyhow::Result<\(\)> \{\n",
    "pub fn main_set_option(key: String, value: String) -> hbb_common::anyhow::Result<()> {\n"
    '    if key == "ephemeral-autostart" {\n'
    "        crate::common::sync_autostart(&value);\n"
    "    }\n",
    "main_set_option autostart hook",
)
anchor = """    if custom_client_config.is_empty() {
        crate::load_custom_client();
    } else {
        crate::read_custom_client(custom_client_config);
    }
"""
if anchor not in s:
    sys.exit("patch anchor not found: flutter_ffi initialize()")
if "apply_builtin_policy" not in s:
    s = s.replace(
        anchor,
        anchor + "    crate::common::apply_builtin_policy();\n",
        1,
    )
write(rel, s)


# ---------------------------------------------------------------------------
# 4. Portable packer: unique temp extraction dir, no permanent Loading window,
#    export the persistent data folder + launcher path, remove only the
#    extracted payload on exit (config is deliberately kept).
# ---------------------------------------------------------------------------
rel = "libs/portable/src/main.rs"
s = read(rel)
old_dir = """    let dir = if let Some(dir) = dir {
        dir
    } else {
        // home dir
        if let Some(dir) = dirs::data_local_dir() {
            dir.join(app_dir_name(&reader.exe))
        } else {
            eprintln!("not found data local dir");
            return None;
        }
    };
"""
new_dir = """    let dir = if let Some(dir) = dir {
        dir
    } else {
        // Extract into a unique temp directory; only the persistent data
        // folder beside the launcher holds state across runs.
        std::env::temp_dir().join(format!(
            "rd-{}-{}",
            app_dir_name(&reader.exe),
            std::process::id()
        ))
    };
"""
if old_dir not in s:
    sys.exit("patch anchor not found: portable setup() extraction dir")
s = s.replace(old_dir, new_dir, 1)

old_ui = """    let mut ts = 0;
    if clear || !is_timestamp_matches(&dir, &mut ts) {
        #[cfg(windows)]
        if _args.is_empty() {
            *_ui = true;
            ui::setup();
        }
        std::fs::remove_dir_all(&dir).ok();
    }
"""
new_ui = """    let mut ts = 0;
    if clear || !is_timestamp_matches(&dir, &mut ts) {
        // ui::setup() spawns a purple "Loading..." window with no exit path.
        // Our extraction dir is unique per launch, so the timestamp never
        // matches and it would otherwise appear on EVERY run.
        #[cfg(windows)]
        if _args.is_empty() {
            *_ui = true;
        }
        std::fs::remove_dir_all(&dir).ok();
    }
"""
if old_ui not in s:
    sys.exit("patch anchor not found: portable setup() loading window")
s = s.replace(old_ui, new_ui, 1)

s = sub_once(
    s,
    r"fn execute\(path: PathBuf, args: Vec<String>, _ui: bool\) \{\n",
    "fn execute(path: PathBuf, args: Vec<String>, _ui: bool) {\n"
    "    let extracted_dir = path.parent().map(|p| p.to_path_buf());\n",
    "portable execute() signature",
)
s = sub_once(
    s,
    r"    cmd\.env\(APPNAME_RUNTIME_ENV_KEY, exe_name\);\n",
    "    cmd.env(APPNAME_RUNTIME_ENV_KEY, exe_name);\n"
    "    // Persistent data folder next to the single-file exe: ID and the\n"
    "    // user-chosen permanent password live here and survive restarts.\n"
    "    let data_dir = exe\n"
    "        .parent()\n"
    "        .map(|p| p.to_path_buf())\n"
    "        .unwrap_or_else(std::env::temp_dir)\n"
    f'        .join("{DATA_DIR_NAME}");\n'
    "    let _ = std::fs::create_dir_all(&data_dir);\n"
    '    cmd.env("RUSTDESK_DATA_DIR", &data_dir);\n'
    "    // Absolute path of the exe the user double-clicked, used for the\n"
    "    // HKCU Run entry (the extracted payload disappears on exit).\n"
    '    cmd.env("RUSTDESK_LAUNCHER_EXE", &exe);\n',
    "portable execute() data dir + launcher env",
)

old_tail = """    let _child = cmd.spawn();

    #[cfg(windows)]
    if _ui {
        match _child {
            Ok(child) => unsafe {
                winapi::um::winuser::AllowSetForegroundWindow(child.id() as u32);
            },
            Err(e) => {
                eprintln!("{:?}", e);
            }
        }
    }
}
"""
new_tail = """    let child = cmd.spawn();

    #[cfg(windows)]
    if _ui {
        match &child {
            Ok(c) => unsafe {
                winapi::um::winuser::AllowSetForegroundWindow(c.id() as u32);
            },
            Err(e) => {
                eprintln!("{:?}", e);
            }
        }
    }
    if let Ok(mut c) = child {
        let _ = c.wait();
    }
    // Remove only the extracted payload. Config lives in RUSTDESK_DATA_DIR
    // beside the exe and is intentionally kept so passwords survive restarts.
    if let Some(dir) = extracted_dir {
        let _ = std::fs::remove_dir_all(dir);
    }
}
"""
if old_tail not in s:
    sys.exit("patch anchor not found: portable execute() tail")
s = s.replace(old_tail, new_tail, 1)
write(rel, s)


# ---------------------------------------------------------------------------
# 5. Desktop home page: ID board, password, "set my own password" button,
#    "start on boot" switch and one hint line. The ID board "..." popup menu
#    (the last visible route into Settings) is removed as well.
# ---------------------------------------------------------------------------
rel = "flutter/lib/desktop/pages/desktop_home_page.dart"
s = read(rel)
if "buildPopupMenu(context)" not in s:
    sys.exit("patch anchor not found: ID board popup menu")
s = s.replace("buildPopupMenu(context)", "const SizedBox.shrink()", 1)

# Keep `isIncomingOnly`: buildLeftPane() still uses it for the pane width.
start = s.find("    final children = <Widget>[")
end_marker = "    final textColor = Theme.of(context).textTheme.titleLarge?.color;"
end = s.find(end_marker, start)
if start < 0 or end < 0:
    sys.exit("patch anchor not found: desktop_home_page buildLeftPane()")

dart_hint = json.dumps(HINT, ensure_ascii=True)
# A single button that opens ONLY the official password dialog. It never opens
# the settings page, so all settings entries stay removed.
set_password_button = (
    "      const SizedBox(height: 8),\n"
    "      OutlinedButton.icon(\n"
    "        onPressed: () {\n"
    "          setPasswordDialog();\n"
    "        },\n"
    "        icon: const Icon(Icons.lock_outline, size: 16),\n"
    f"        label: Text({json.dumps('设置自己的固定连接密码', ensure_ascii=True)},\n"
    "            style: const TextStyle(fontSize: 13)),\n"
    "      ),\n"
)
# Auto-start switch. State lives in the persistent config (RustDesk option),
# so it is remembered across restarts; the Rust side writes HKCU\\...\\Run.
autostart_line = (
    "      const SizedBox(height: 4),\n"
    "      StatefulBuilder(\n"
    "        builder: (context, setState) {\n"
    "          final enabled =\n"
    "              bind.mainGetOptionSync(key: 'ephemeral-autostart') == 'Y';\n"
    "          return Row(\n"
    "            mainAxisSize: MainAxisSize.min,\n"
    "            mainAxisAlignment: MainAxisAlignment.center,\n"
    "            children: [\n"
    f"              Text({json.dumps('开机自动启动', ensure_ascii=True)},\n"
    "                  style: const TextStyle(fontSize: 13, color: Colors.grey)),\n"
    "              Switch(\n"
    "                value: enabled,\n"
    "                onChanged: (v) async {\n"
    "                  await bind.mainSetOption(\n"
    "                      key: 'ephemeral-autostart', value: v ? 'Y' : '');\n"
    "                  setState(() {});\n"
    "                },\n"
    "              ),\n"
    "            ],\n"
    "          );\n"
    "        },\n"
    "      ),\n"
)
if PRESET_PASSWORD:
    # 1.5.x stores the permanent password as a hash and cannot show it back.
    # Here the plaintext is a compile-time constant, so we can display it.
    # Fixed mode and user-chosen mode are mutually exclusive.
    dart_password = json.dumps(PRESET_PASSWORD, ensure_ascii=False).replace("$", r"\$")
    password_line = (
        "      const SizedBox(height: 12),\n"
        "      Text('连接密码', style: const TextStyle(fontSize: 12, color: Colors.grey)),\n"
        f"      SelectableText({dart_password},\n"
        "          style: const TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),\n"
    )
    set_password_line = ""
elif SHOW_PW:
    password_line = "      if (!isOutgoingOnly) buildPasswordBoard(context),\n"
    set_password_line = set_password_button
else:
    password_line = "      const SizedBox(height: 12),\n"
    set_password_line = set_password_button
new_block = f"""    final children = <Widget>[
      if (!isOutgoingOnly) buildIDBoard(context),
{password_line}{set_password_line}{autostart_line}      const SizedBox(height: 12),
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Text(
          {dart_hint},
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 12, color: Colors.grey),
        ),
      ),
    ];
"""
s = s[:start] + new_block + s[end:]
write(rel, s)


# ---------------------------------------------------------------------------
# 6. Remove every remaining route into the settings page. `onAddSetting` is
#    called from the tab bar, tray/IPC handlers and shortcuts; making it a
#    no-op closes all of them at once.
# ---------------------------------------------------------------------------
rel = "flutter/lib/desktop/pages/desktop_tab_page.dart"
s = read(rel)
old_add = """  static void onAddSetting(
      {SettingsTabKey initialPage = SettingsTabKey.general}) {
    try {
      DesktopTabController tabController = Get.find<DesktopTabController>();
      tabController.add(TabInfo(
          key: kTabLabelSettingPage,
          label: kTabLabelSettingPage,
          selectedIcon: Icons.build_sharp,
          unselectedIcon: Icons.build_outlined,
          page: DesktopSettingPage(
            key: const ValueKey(kTabLabelSettingPage),
            initialTabkey: initialPage,
          )));
    } catch (e) {
      debugPrintStack(label: '$e');
    }
  }
"""
new_add = """  static void onAddSetting(
      {SettingsTabKey initialPage = SettingsTabKey.general}) {
    // Controlled-only client: the settings page is compiled out entirely.
    // Keep the signature so every existing caller still compiles.
  }
"""
if old_add not in s:
    sys.exit("patch anchor not found: DesktopTabPage.onAddSetting")
s = s.replace(old_add, new_add, 1)

old_tail = """              tail: Offstage(
                offstage: bind.isIncomingOnly() || bind.isDisableSettings(),
                child: ActionIcon(
                  message: 'Settings',
                  icon: IconFont.menu,
                  onTap: DesktopTabPage.onAddSetting,
                  isClose: false,
                ),
              ),
"""
if old_tail not in s:
    sys.exit("patch anchor not found: settings tab button")
s = s.replace(old_tail, "              tail: const SizedBox.shrink(),\n", 1)
write(rel, s)

print("[ok] all patches applied")
