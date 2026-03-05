#ifndef WEBVIEW_BRIDGE_H_
#define WEBVIEW_BRIDGE_H_

#include <flutter_linux/flutter_linux.h>
#include <gtk/gtk.h>

/// Initialize the WebKitGTK bridge.
/// Creates a method channel and event channel on the given messenger,
/// and attaches a hidden WebKitWebView to the given overlay container.
void webview_bridge_init(FlBinaryMessenger* messenger, GtkOverlay* overlay);

/// Clean up the WebKitGTK bridge resources.
void webview_bridge_dispose(void);

#endif  // WEBVIEW_BRIDGE_H_
