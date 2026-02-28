#include "my_application.h"

#include <cstdlib>

int main(int argc, char** argv) {
  // WebKitGTK + NVIDIA/Wayland compatibility:
  // DMA-BUF renderer causes blank screens and crashes on NVIDIA GPUs.
  setenv("WEBKIT_DISABLE_DMABUF_RENDERER", "1", 1);
  // Explicit sync can cause Wayland protocol errors on NVIDIA.
  setenv("__NV_DISABLE_EXPLICIT_SYNC", "1", 1);
  // Force Mesa (AMD iGPU) EGL for both Flutter and WebKitGTK — prevents
  // them from picking the NVIDIA dGPU, which has DMABUF/Wayland issues.
  setenv("__EGL_VENDOR_LIBRARY_FILENAMES",
         "/usr/share/glvnd/egl_vendor.d/50_mesa.json", 0);

  g_autoptr(MyApplication) app = my_application_new();
  return g_application_run(G_APPLICATION(app), argc, argv);
}
