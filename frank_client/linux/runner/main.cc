#include "my_application.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

const char *env_or_unset(const char *key) {
  const char *value = std::getenv(key);
  return value != nullptr ? value : "(unset)";
}

void set_default_env(const char *key, const char *value) {
  if (std::getenv(key) == nullptr) {
    setenv(key, value, 1);
  }
}

} // namespace

int main(int argc, char **argv) {
  // Linux rendering compatibility defaults:
  // - Respect caller-provided env vars (do not override explicit choices).
  // - Keep safe defaults for NVIDIA/Wayland unless disabled.
  const char *safe_mode = std::getenv("FRANK_LINUX_SAFE_MODE");
  const bool use_safe_mode =
      (safe_mode == nullptr) || (std::strcmp(safe_mode, "0") != 0 &&
                                 std::strcmp(safe_mode, "false") != 0);

  if (use_safe_mode) {
    // DMA-BUF renderer can cause blank screens/crashes on some NVIDIA setups.
    set_default_env("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    // Explicit sync can trigger Wayland protocol errors on some NVIDIA stacks.
    set_default_env("__NV_DISABLE_EXPLICIT_SYNC", "1");
  }

  // Optional legacy behavior: force Mesa EGL (usually AMD iGPU) when requested.
  // This is now opt-in so hybrid systems can use dGPU by default.
  const char *force_mesa = std::getenv("FRANK_FORCE_MESA_EGL");
  if (force_mesa != nullptr && (std::strcmp(force_mesa, "1") == 0 ||
                                std::strcmp(force_mesa, "true") == 0)) {
    set_default_env("__EGL_VENDOR_LIBRARY_FILENAMES",
                    "/usr/share/glvnd/egl_vendor.d/50_mesa.json");
  }

  std::fprintf(stderr,
               "[frank_client] FRANK_LINUX_SAFE_MODE=%s "
               "WEBKIT_DISABLE_DMABUF_RENDERER=%s "
               "__NV_DISABLE_EXPLICIT_SYNC=%s "
               "FRANK_FORCE_MESA_EGL=%s "
               "__EGL_VENDOR_LIBRARY_FILENAMES=%s\n",
               env_or_unset("FRANK_LINUX_SAFE_MODE"),
               env_or_unset("WEBKIT_DISABLE_DMABUF_RENDERER"),
               env_or_unset("__NV_DISABLE_EXPLICIT_SYNC"),
               env_or_unset("FRANK_FORCE_MESA_EGL"),
               env_or_unset("__EGL_VENDOR_LIBRARY_FILENAMES"));

  g_autoptr(MyApplication) app = my_application_new();
  return g_application_run(G_APPLICATION(app), argc, argv);
}
