#include "my_application.h"

#include <cstdlib>

int main(int argc, char** argv) {
  // WebKitGTK + NVIDIA/Wayland compatibility:
  // DMA-BUF renderer causes blank screens and crashes on NVIDIA GPUs.
  setenv("WEBKIT_DISABLE_DMABUF_RENDERER", "1", 0);
  // Explicit sync can cause Wayland protocol errors on NVIDIA.
  setenv("__NV_DISABLE_EXPLICIT_SYNC", "1", 0);
  // Force Skia CPU rendering — avoids NVIDIA GPU rendering + SHM buffer
  // copy overhead when DMABUF is disabled on Wayland. CPU Skia uses
  // multithreaded tile painting and writes directly to SHM buffers.
  setenv("WEBKIT_SKIA_ENABLE_CPU_RENDERING", "1", 0);
  // Disable accelerated compositing — CSS transforms/animations would
  // otherwise trigger GPU compositing with expensive SHM fallback.
  setenv("WEBKIT_DISABLE_COMPOSITING_MODE", "1", 0);

  g_autoptr(MyApplication) app = my_application_new();
  return g_application_run(G_APPLICATION(app), argc, argv);
}
