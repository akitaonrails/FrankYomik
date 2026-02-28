#include "webview_bridge.h"

#include <cstring>
#include <map>
#include <string>
#include <sys/stat.h>

#include <gdk/gdk.h>
#include <webkit2/webkit2.h>

// --- Main webview globals ---
static GtkOverlay* g_overlay = nullptr;
static WebKitWebView* g_webview = nullptr;
static GtkWidget* g_webview_widget = nullptr;
static FlMethodChannel* g_method_channel = nullptr;
static FlEventChannel* g_event_channel = nullptr;
static gboolean g_listening = FALSE;
static WebKitUserContentManager* g_content_manager = nullptr;
static WebKitWebContext* g_web_context = nullptr;

// Track registered JS handler names so we can unregister them.
static std::map<std::string, gulong> g_handler_signals;

// --- Background webview globals ---
static WebKitWebView* g_bg_webview = nullptr;
static GtkWidget* g_bg_webview_widget = nullptr;
static WebKitUserContentManager* g_bg_content_manager = nullptr;
static FlMethodChannel* g_bg_method_channel = nullptr;
static FlEventChannel* g_bg_event_channel = nullptr;
static gboolean g_bg_listening = FALSE;
static std::map<std::string, gulong> g_bg_handler_signals;

// The JS shim injected on every page load to emulate
// window.flutter_inappwebview.callHandler().
static const char* JS_BRIDGE_SHIM = R"JS(
(function() {
  if (window.flutter_inappwebview) return;
  window.flutter_inappwebview = {
    callHandler: function(name) {
      var args = Array.prototype.slice.call(arguments, 1);
      window.webkit.messageHandlers[name].postMessage(JSON.stringify(args));
    }
  };
})();
)JS";

// Anti-bot script injected at document-start to mask WebView fingerprints.
// Each override is wrapped in try-catch — some properties may be non-configurable
// in certain WebKit versions, and a single throw would abort the entire script.
static const char* JS_ANTI_BOT = R"JS(
(function() {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
  try { Object.defineProperty(navigator, 'plugins', {
    get: () => [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
      { name: 'Native Client', filename: 'internal-nacl-plugin' },
    ]
  }); } catch(e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'ja'] }); } catch(e) {}
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch(e) {}
  try { Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }); } catch(e) {}
})();
)JS";

// --- Forward declarations ---
static void on_method_call(FlMethodChannel* channel, FlMethodCall* method_call,
                           gpointer user_data);
static void on_bg_method_call(FlMethodChannel* channel,
                              FlMethodCall* method_call, gpointer user_data);

// --- Helper: send event to Dart (main webview) ---
static void send_event(const char* type, FlValue* data) {
  if (!g_listening || g_event_channel == nullptr) return;
  g_autoptr(FlValue) event = fl_value_new_map();
  fl_value_set_string_take(event, "type", fl_value_new_string(type));
  if (data != nullptr) {
    fl_value_set_string_take(event, "data", fl_value_ref(data));
  }
  g_autoptr(GError) error = nullptr;
  fl_event_channel_send(g_event_channel, event, nullptr, &error);
  if (error != nullptr) {
    g_warning("Failed to send event: %s", error->message);
  }
}

// --- Helper: send event to Dart (background webview) ---
static void send_bg_event(const char* type, FlValue* data) {
  if (!g_bg_listening || g_bg_event_channel == nullptr) return;
  g_autoptr(FlValue) event = fl_value_new_map();
  fl_value_set_string_take(event, "type", fl_value_new_string(type));
  if (data != nullptr) {
    fl_value_set_string_take(event, "data", fl_value_ref(data));
  }
  g_autoptr(GError) error = nullptr;
  fl_event_channel_send(g_bg_event_channel, event, nullptr, &error);
  if (error != nullptr) {
    g_warning("Failed to send bg event: %s", error->message);
  }
}

// --- Main WebKitWebView signal handlers ---
static void on_load_changed(WebKitWebView* web_view, WebKitLoadEvent event,
                            gpointer user_data) {
  if (event == WEBKIT_LOAD_FINISHED) {
    const gchar* uri = webkit_web_view_get_uri(web_view);
    g_autoptr(FlValue) data = fl_value_new_map();
    fl_value_set_string_take(data, "url",
                             fl_value_new_string(uri ? uri : ""));
    send_event("onLoadStop", data);
  }
}

static void on_uri_changed(GObject* object, GParamSpec* pspec,
                           gpointer user_data) {
  WebKitWebView* web_view = WEBKIT_WEB_VIEW(object);
  const gchar* uri = webkit_web_view_get_uri(web_view);
  g_autoptr(FlValue) data = fl_value_new_map();
  fl_value_set_string_take(data, "url",
                           fl_value_new_string(uri ? uri : ""));
  send_event("onUpdateVisitedHistory", data);
}

// --- Handle window.open() / target="_blank" ---
// Amazon's library grid may open the reader via a new window request.
// Redirect to the same WebView instead of silently dropping it.
static GtkWidget* on_create(WebKitWebView* web_view,
                             WebKitNavigationAction* navigation_action,
                             gpointer user_data) {
  WebKitURIRequest* request =
      webkit_navigation_action_get_request(navigation_action);
  const gchar* uri = webkit_uri_request_get_uri(request);

  if (uri != nullptr && g_webview != nullptr) {
    gchar* uri_copy = g_strdup(uri);
    g_idle_add(
        [](gpointer data) -> gboolean {
          gchar* u = static_cast<gchar*>(data);
          if (g_webview != nullptr) {
            webkit_web_view_load_uri(g_webview, u);
          }
          g_free(u);
          return G_SOURCE_REMOVE;
        },
        uri_copy);
  }

  return nullptr; // Don't create a new window
}

// --- Main webview JS message handler callback ---
static void on_script_message(WebKitUserContentManager* manager,
                              WebKitJavascriptResult* js_result,
                              gpointer user_data) {
  const char* handler_name = static_cast<const char*>(user_data);
  JSCValue* value = webkit_javascript_result_get_js_value(js_result);
  gchar* json = jsc_value_to_string(value);

  g_autoptr(FlValue) data = fl_value_new_map();
  fl_value_set_string_take(data, "name",
                           fl_value_new_string(handler_name));
  fl_value_set_string_take(data, "args",
                           fl_value_new_string(json ? json : "[]"));
  send_event("onJavaScriptHandler", data);

  g_free(json);
}

// --- Background webview signal handlers ---
static void on_bg_load_changed(WebKitWebView* web_view,
                                WebKitLoadEvent event, gpointer user_data) {
  if (event == WEBKIT_LOAD_FINISHED) {
    const gchar* uri = webkit_web_view_get_uri(web_view);
    g_autoptr(FlValue) data = fl_value_new_map();
    fl_value_set_string_take(data, "url",
                             fl_value_new_string(uri ? uri : ""));
    send_bg_event("onLoadStop", data);
  }
}

static void on_bg_script_message(WebKitUserContentManager* manager,
                                  WebKitJavascriptResult* js_result,
                                  gpointer user_data) {
  const char* handler_name = static_cast<const char*>(user_data);
  JSCValue* value = webkit_javascript_result_get_js_value(js_result);
  gchar* json = jsc_value_to_string(value);

  g_autoptr(FlValue) data = fl_value_new_map();
  fl_value_set_string_take(data, "name",
                           fl_value_new_string(handler_name));
  fl_value_set_string_take(data, "args",
                           fl_value_new_string(json ? json : "[]"));
  send_bg_event("onJavaScriptHandler", data);

  g_free(json);
}

// ============================================================
// Main webview method channel handlers
// ============================================================

static void handle_create(FlMethodCall* method_call) {
  FlValue* args = fl_method_call_get_args(method_call);
  const char* url = fl_value_get_string(fl_value_lookup_string(args, "url"));
  FlValue* ua_val = fl_value_lookup_string(args, "userAgent");
  const char* user_agent =
      (ua_val != nullptr && fl_value_get_type(ua_val) == FL_VALUE_TYPE_STRING)
          ? fl_value_get_string(ua_val)
          : nullptr;

  if (g_webview == nullptr) {
    g_content_manager = webkit_user_content_manager_new();

    // Inject anti-bot script before any page JS runs.
    WebKitUserScript* anti_bot_script = webkit_user_script_new(
        JS_ANTI_BOT, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES,
        WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, nullptr, nullptr);
    webkit_user_content_manager_add_script(g_content_manager, anti_bot_script);
    webkit_user_script_unref(anti_bot_script);

    // Inject the JS bridge shim on every page (document start).
    WebKitUserScript* script = webkit_user_script_new(
        JS_BRIDGE_SHIM, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES,
        WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, nullptr, nullptr);
    webkit_user_content_manager_add_script(g_content_manager, script);
    webkit_user_script_unref(script);

    // Persistent data manager — cookies, localStorage, IndexedDB survive restarts.
    // Directories are created with 0700 (owner-only) to protect session tokens.
    gchar* data_dir =
        g_build_filename(g_get_user_data_dir(), "frank_client", "webview", nullptr);
    gchar* cache_dir =
        g_build_filename(g_get_user_cache_dir(), "frank_client", "webview", nullptr);
    g_mkdir_with_parents(data_dir, 0700);
    g_mkdir_with_parents(cache_dir, 0700);
    // Also lock down the parent frank_client dirs.
    gchar* data_parent = g_build_filename(g_get_user_data_dir(), "frank_client", nullptr);
    gchar* cache_parent = g_build_filename(g_get_user_cache_dir(), "frank_client", nullptr);
    chmod(data_parent, 0700);
    chmod(cache_parent, 0700);
    g_free(data_parent);
    g_free(cache_parent);

    WebKitWebsiteDataManager* data_manager = webkit_website_data_manager_new(
        "base-data-directory", data_dir,
        "base-cache-directory", cache_dir,
        nullptr);

    g_web_context =
        webkit_web_context_new_with_website_data_manager(data_manager);

    // Persistent cookie storage (SQLite).
    WebKitCookieManager* cookie_manager =
        webkit_web_context_get_cookie_manager(g_web_context);
    gchar* cookie_file = g_build_filename(data_dir, "cookies.sqlite", nullptr);
    webkit_cookie_manager_set_persistent_storage(
        cookie_manager, cookie_file,
        WEBKIT_COOKIE_PERSISTENT_STORAGE_SQLITE);
    webkit_cookie_manager_set_accept_policy(
        cookie_manager, WEBKIT_COOKIE_POLICY_ACCEPT_ALWAYS);
    g_free(cookie_file);
    g_free(data_dir);
    g_free(cache_dir);

    // Create WebView with persistent context + user content manager.
    g_webview = WEBKIT_WEB_VIEW(g_object_new(
        WEBKIT_TYPE_WEB_VIEW,
        "web-context", g_web_context,
        "user-content-manager", g_content_manager,
        nullptr));
    g_webview_widget = GTK_WIDGET(g_webview);

    g_object_unref(data_manager);

    g_signal_connect(g_webview, "load-changed",
                     G_CALLBACK(on_load_changed), nullptr);
    g_signal_connect(g_webview, "notify::uri",
                     G_CALLBACK(on_uri_changed), nullptr);
    g_signal_connect(g_webview, "create",
                     G_CALLBACK(on_create), nullptr);

    gtk_overlay_add_overlay(g_overlay, g_webview_widget);
  }

  // Apply user agent if provided.
  if (user_agent != nullptr) {
    WebKitSettings* settings = webkit_web_view_get_settings(g_webview);
    webkit_settings_set_user_agent(settings, user_agent);
  }

  // Enable JavaScript, storage APIs, and window.open support.
  WebKitSettings* settings = webkit_web_view_get_settings(g_webview);
  webkit_settings_set_enable_javascript(settings, TRUE);
  webkit_settings_set_enable_html5_database(settings, TRUE);
  webkit_settings_set_enable_html5_local_storage(settings, TRUE);
  webkit_settings_set_javascript_can_open_windows_automatically(settings, TRUE);

  // Disable GPU-accelerated compositing — on NVIDIA+Wayland with DMABUF
  // disabled, GPU compositing causes expensive SHM buffer copies at
  // full-screen resolution. CPU compositing with Skia is faster here.
  webkit_settings_set_hardware_acceleration_policy(
      settings, WEBKIT_HARDWARE_ACCELERATION_POLICY_NEVER);

  webkit_web_view_load_uri(g_webview, url);
  gtk_widget_show(g_webview_widget);

  g_autoptr(FlMethodResponse) response =
      FL_METHOD_RESPONSE(fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

static void handle_set_frame(FlMethodCall* method_call) {
  // Not used for overlay positioning in GTK — the WebView fills the overlay.
  g_autoptr(FlMethodResponse) response =
      FL_METHOD_RESPONSE(fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

// --- Shared JS evaluation callback (works for both main and bg webviews) ---
static void on_js_finished(GObject* object, GAsyncResult* result,
                           gpointer user_data) {
  FlMethodCall* method_call = FL_METHOD_CALL(user_data);
  GError* error = nullptr;
  JSCValue* value = webkit_web_view_evaluate_javascript_finish(
      WEBKIT_WEB_VIEW(object), result, &error);

  if (error != nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new("JS_ERROR", error->message, nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    g_error_free(error);
  } else {
    gchar* str = nullptr;
    g_autoptr(FlValue) result_val = nullptr;

    if (jsc_value_is_string(value)) {
      str = jsc_value_to_string(value);
      result_val = fl_value_new_string(str);
    } else if (jsc_value_is_boolean(value)) {
      result_val = fl_value_new_bool(jsc_value_to_boolean(value));
    } else if (jsc_value_is_number(value)) {
      result_val = fl_value_new_float(jsc_value_to_double(value));
    } else if (jsc_value_is_null(value) || jsc_value_is_undefined(value)) {
      result_val = fl_value_new_null();
    } else {
      str = jsc_value_to_string(value);
      result_val = fl_value_new_string(str);
    }

    g_autoptr(FlMethodResponse) response =
        FL_METHOD_RESPONSE(fl_method_success_response_new(result_val));
    fl_method_call_respond(method_call, response, nullptr);

    g_free(str);
    g_object_unref(value);
  }

  g_object_unref(method_call);
}

static void handle_evaluate_javascript(FlMethodCall* method_call) {
  if (g_webview == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new("NO_WEBVIEW", "WebView not created", nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  FlValue* args = fl_method_call_get_args(method_call);
  const char* source =
      fl_value_get_string(fl_value_lookup_string(args, "source"));
  gsize length = strlen(source);

  // Keep method_call alive until the async callback.
  g_object_ref(method_call);
  webkit_web_view_evaluate_javascript(g_webview, source, length, nullptr,
                                      nullptr, nullptr, on_js_finished,
                                      method_call);
}

// --- Method channel: takeScreenshot ---
static void on_snapshot_finished(GObject* object, GAsyncResult* result,
                                 gpointer user_data) {
  FlMethodCall* method_call = FL_METHOD_CALL(user_data);
  GError* error = nullptr;
  cairo_surface_t* surface = webkit_web_view_get_snapshot_finish(
      WEBKIT_WEB_VIEW(object), result, &error);

  if (error != nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new("SNAPSHOT_ERROR", error->message, nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    g_error_free(error);
  } else {
    // Write cairo surface to PNG in memory.
    GByteArray* byte_array = g_byte_array_new();
    cairo_surface_write_to_png_stream(
        surface,
        [](void* closure, const unsigned char* data,
           unsigned int length) -> cairo_status_t {
          g_byte_array_append(static_cast<GByteArray*>(closure), data, length);
          return CAIRO_STATUS_SUCCESS;
        },
        byte_array);

    g_autoptr(FlValue) result_val =
        fl_value_new_uint8_list(byte_array->data, byte_array->len);
    g_autoptr(FlMethodResponse) response =
        FL_METHOD_RESPONSE(fl_method_success_response_new(result_val));
    fl_method_call_respond(method_call, response, nullptr);

    g_byte_array_unref(byte_array);
    cairo_surface_destroy(surface);
  }

  g_object_unref(method_call);
}

static void handle_take_screenshot(FlMethodCall* method_call) {
  if (g_webview == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new("NO_WEBVIEW", "WebView not created", nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  g_object_ref(method_call);
  webkit_web_view_get_snapshot(g_webview, WEBKIT_SNAPSHOT_REGION_VISIBLE,
                               WEBKIT_SNAPSHOT_OPTIONS_NONE, nullptr,
                               on_snapshot_finished, method_call);
}

static void handle_add_js_handler(FlMethodCall* method_call) {
  if (g_content_manager == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new("NO_WEBVIEW", "WebView not created", nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  FlValue* args = fl_method_call_get_args(method_call);
  const char* name =
      fl_value_get_string(fl_value_lookup_string(args, "name"));

  // Skip if already registered.
  if (g_handler_signals.find(name) != g_handler_signals.end()) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_success_response_new(fl_value_new_bool(TRUE)));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  // We need a persistent copy of the name for the signal callback.
  char* name_copy = g_strdup(name);

  gboolean registered =
      webkit_user_content_manager_register_script_message_handler(
          g_content_manager, name);

  if (registered) {
    gchar* signal_name =
        g_strdup_printf("script-message-received::%s", name);
    gulong sig_id = g_signal_connect(g_content_manager, signal_name,
                                     G_CALLBACK(on_script_message), name_copy);
    g_handler_signals[name] = sig_id;
    g_free(signal_name);
  }

  g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
      fl_method_success_response_new(fl_value_new_bool(registered)));
  fl_method_call_respond(method_call, response, nullptr);
}

// --- Method channel: destroy (main webview) ---
static void handle_destroy(FlMethodCall* method_call) {
  if (g_webview_widget != nullptr) {
    gtk_widget_hide(g_webview_widget);
    gtk_container_remove(GTK_CONTAINER(g_overlay), g_webview_widget);
    g_webview = nullptr;
    g_webview_widget = nullptr;
    g_content_manager = nullptr;
    // Only free the web context if the bg webview is also gone.
    if (g_web_context != nullptr && g_bg_webview == nullptr) {
      g_object_unref(g_web_context);
      g_web_context = nullptr;
    }
    g_handler_signals.clear();
  }

  g_autoptr(FlMethodResponse) response =
      FL_METHOD_RESPONSE(fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

// ============================================================
// Background webview method channel handlers
// ============================================================

static void handle_bg_create(FlMethodCall* method_call) {
  FlValue* args = fl_method_call_get_args(method_call);
  const char* url = fl_value_get_string(fl_value_lookup_string(args, "url"));

  if (g_web_context == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new(
            "NO_CONTEXT",
            "Main webview must be created first (shared web context)",
            nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  if (g_bg_webview != nullptr) {
    // Already exists — just navigate to new URL.
    webkit_web_view_load_uri(g_bg_webview, url);
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_success_response_new(fl_value_new_bool(TRUE)));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  // Separate content manager so JS handlers don't clash with main webview.
  g_bg_content_manager = webkit_user_content_manager_new();

  // Inject anti-bot + JS bridge shim on bg webview too.
  WebKitUserScript* anti_bot_script = webkit_user_script_new(
      JS_ANTI_BOT, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES,
      WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, nullptr, nullptr);
  webkit_user_content_manager_add_script(g_bg_content_manager, anti_bot_script);
  webkit_user_script_unref(anti_bot_script);

  WebKitUserScript* bridge_script = webkit_user_script_new(
      JS_BRIDGE_SHIM, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES,
      WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, nullptr, nullptr);
  webkit_user_content_manager_add_script(g_bg_content_manager, bridge_script);
  webkit_user_script_unref(bridge_script);

  // Create bg webview reusing the same web context (shared cookies/session).
  g_bg_webview = WEBKIT_WEB_VIEW(g_object_new(
      WEBKIT_TYPE_WEB_VIEW,
      "web-context", g_web_context,
      "user-content-manager", g_bg_content_manager,
      nullptr));
  g_bg_webview_widget = GTK_WIDGET(g_bg_webview);

  // Copy user agent from main webview.
  if (g_webview != nullptr) {
    WebKitSettings* main_settings = webkit_web_view_get_settings(g_webview);
    const gchar* ua = webkit_settings_get_user_agent(main_settings);
    if (ua != nullptr) {
      WebKitSettings* bg_settings = webkit_web_view_get_settings(g_bg_webview);
      webkit_settings_set_user_agent(bg_settings, ua);
    }
  }

  // Enable JS and storage on bg webview.
  WebKitSettings* bg_settings = webkit_web_view_get_settings(g_bg_webview);
  webkit_settings_set_enable_javascript(bg_settings, TRUE);
  webkit_settings_set_enable_html5_database(bg_settings, TRUE);
  webkit_settings_set_enable_html5_local_storage(bg_settings, TRUE);
  webkit_settings_set_hardware_acceleration_policy(
      bg_settings, WEBKIT_HARDWARE_ACCELERATION_POLICY_NEVER);

  g_signal_connect(g_bg_webview, "load-changed",
                   G_CALLBACK(on_bg_load_changed), nullptr);

  // Add to overlay but make invisible and non-interactive.
  // Fixed 1024x768 so Kindle renders blob images at normal dimensions
  // without compositing a full-screen transparent layer (GPU overhead).
  // sensitive=FALSE prevents focus stealing (WebKitGTK grabs focus on load).
  // handle_bg_send_key() temporarily enables sensitivity to deliver key events.
  gtk_overlay_add_overlay(g_overlay, g_bg_webview_widget);
  gtk_widget_set_size_request(g_bg_webview_widget, 1024, 768);
  gtk_widget_set_halign(g_bg_webview_widget, GTK_ALIGN_START);
  gtk_widget_set_valign(g_bg_webview_widget, GTK_ALIGN_START);
  gtk_widget_set_opacity(g_bg_webview_widget, 0.0);
  gtk_widget_set_sensitive(g_bg_webview_widget, FALSE);
  gtk_widget_set_can_focus(g_bg_webview_widget, FALSE);

  // Let input events pass through to the main webview underneath.
  gtk_overlay_set_overlay_pass_through(g_overlay, g_bg_webview_widget, TRUE);

  gtk_widget_show(g_bg_webview_widget);

  // Make the bg webview's GDK window pass-through for input events.
  // GTK's overlay pass_through only affects GTK-level event routing, but
  // GDK dispatches mouse events to the topmost GdkWindow at the pointer
  // position BEFORE GTK sees them. This ensures the bg webview's GDK window
  // (and its WebKit child windows) never intercept mouse input.
  GdkWindow* bg_gdk_win = gtk_widget_get_window(g_bg_webview_widget);
  if (bg_gdk_win) {
    gdk_window_set_pass_through(bg_gdk_win, TRUE);
  }

  webkit_web_view_load_uri(g_bg_webview, url);

  g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
      fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

static void handle_bg_evaluate_javascript(FlMethodCall* method_call) {
  if (g_bg_webview == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new(
            "NO_BG_WEBVIEW", "Background WebView not created", nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  FlValue* args = fl_method_call_get_args(method_call);
  const char* source =
      fl_value_get_string(fl_value_lookup_string(args, "source"));
  gsize length = strlen(source);

  g_object_ref(method_call);
  webkit_web_view_evaluate_javascript(g_bg_webview, source, length, nullptr,
                                      nullptr, nullptr, on_js_finished,
                                      method_call);
}

static void handle_bg_add_js_handler(FlMethodCall* method_call) {
  if (g_bg_content_manager == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new(
            "NO_BG_WEBVIEW", "Background WebView not created", nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  FlValue* args = fl_method_call_get_args(method_call);
  const char* name =
      fl_value_get_string(fl_value_lookup_string(args, "name"));

  if (g_bg_handler_signals.find(name) != g_bg_handler_signals.end()) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_success_response_new(fl_value_new_bool(TRUE)));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  char* name_copy = g_strdup(name);

  gboolean registered =
      webkit_user_content_manager_register_script_message_handler(
          g_bg_content_manager, name);

  if (registered) {
    gchar* signal_name =
        g_strdup_printf("script-message-received::%s", name);
    gulong sig_id = g_signal_connect(
        g_bg_content_manager, signal_name,
        G_CALLBACK(on_bg_script_message), name_copy);
    g_bg_handler_signals[name] = sig_id;
    g_free(signal_name);
  }

  g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
      fl_method_success_response_new(fl_value_new_bool(registered)));
  fl_method_call_respond(method_call, response, nullptr);
}

// --- Background webview: send trusted GDK key event ---
// Unlike JS dispatchEvent(), GDK events have isTrusted=true in the browser.
static void handle_bg_send_key(FlMethodCall* method_call) {
  if (g_bg_webview_widget == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new(
            "NO_BG_WEBVIEW", "Background WebView not created", nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  GdkWindow* gdk_window = gtk_widget_get_window(g_bg_webview_widget);
  if (gdk_window == nullptr) {
    g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
        fl_method_error_response_new(
            "NO_WINDOW", "Background WebView has no GDK window", nullptr));
    fl_method_call_respond(method_call, response, nullptr);
    return;
  }

  FlValue* args = fl_method_call_get_args(method_call);
  guint keyval = static_cast<guint>(
      fl_value_get_int(fl_value_lookup_string(args, "keyval")));

  // Look up hardware keycode from keyval via GDK keymap.
  GdkKeymapKey* keys = nullptr;
  gint n_keys = 0;
  guint16 hardware_keycode = 0;
  GdkKeymap* keymap = gdk_keymap_get_for_display(gdk_display_get_default());
  if (gdk_keymap_get_entries_for_keyval(keymap, keyval, &keys, &n_keys) &&
      n_keys > 0) {
    hardware_keycode = static_cast<guint16>(keys[0].keycode);
    g_free(keys);
  }

  // Temporarily enable sensitivity so gtk_widget_event() delivers the event.
  // The bg webview is normally insensitive to prevent focus stealing.
  gtk_widget_set_sensitive(g_bg_webview_widget, TRUE);

  // Send key press event.
  GdkEvent* press = gdk_event_new(GDK_KEY_PRESS);
  GdkEventKey* kp = reinterpret_cast<GdkEventKey*>(press);
  kp->window = gdk_window;
  g_object_ref(kp->window);
  kp->send_event = FALSE;
  kp->time = GDK_CURRENT_TIME;
  kp->state = 0;
  kp->keyval = keyval;
  kp->length = 0;
  kp->string = g_strdup("");
  kp->hardware_keycode = hardware_keycode;
  kp->group = 0;
  kp->is_modifier = FALSE;
  gtk_widget_event(g_bg_webview_widget, press);
  gdk_event_free(press);

  // Send key release event.
  GdkEvent* release = gdk_event_new(GDK_KEY_RELEASE);
  GdkEventKey* kr = reinterpret_cast<GdkEventKey*>(release);
  kr->window = gdk_window;
  g_object_ref(kr->window);
  kr->send_event = FALSE;
  kr->time = GDK_CURRENT_TIME;
  kr->state = 0;
  kr->keyval = keyval;
  kr->length = 0;
  kr->string = g_strdup("");
  kr->hardware_keycode = hardware_keycode;
  kr->group = 0;
  kr->is_modifier = FALSE;
  gtk_widget_event(g_bg_webview_widget, release);
  gdk_event_free(release);

  // Immediately disable sensitivity again to prevent focus stealing.
  gtk_widget_set_sensitive(g_bg_webview_widget, FALSE);

  // Ensure focus stays on the main webview.
  if (g_webview_widget != nullptr) {
    gtk_widget_grab_focus(g_webview_widget);
  }

  g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
      fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

// --- Background webview: destroy ---
static void handle_bg_destroy(FlMethodCall* method_call) {
  if (g_bg_webview_widget != nullptr) {
    gtk_widget_hide(g_bg_webview_widget);
    gtk_container_remove(GTK_CONTAINER(g_overlay), g_bg_webview_widget);
    g_bg_webview = nullptr;
    g_bg_webview_widget = nullptr;
    g_bg_content_manager = nullptr;
    g_bg_handler_signals.clear();
    // Only free the web context if the main webview is also gone.
    if (g_web_context != nullptr && g_webview == nullptr) {
      g_object_unref(g_web_context);
      g_web_context = nullptr;
    }
  }

  g_autoptr(FlMethodResponse) response = FL_METHOD_RESPONSE(
      fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

// ============================================================
// Method channel dispatchers
// ============================================================

static void on_method_call(FlMethodChannel* channel, FlMethodCall* method_call,
                           gpointer user_data) {
  const gchar* method = fl_method_call_get_name(method_call);

  if (strcmp(method, "create") == 0) {
    handle_create(method_call);
  } else if (strcmp(method, "setFrame") == 0) {
    handle_set_frame(method_call);
  } else if (strcmp(method, "evaluateJavascript") == 0) {
    handle_evaluate_javascript(method_call);
  } else if (strcmp(method, "takeScreenshot") == 0) {
    handle_take_screenshot(method_call);
  } else if (strcmp(method, "addJavaScriptHandler") == 0) {
    handle_add_js_handler(method_call);
  } else if (strcmp(method, "destroy") == 0) {
    handle_destroy(method_call);
  } else {
    g_autoptr(FlMethodResponse) response =
        FL_METHOD_RESPONSE(fl_method_not_implemented_response_new());
    fl_method_call_respond(method_call, response, nullptr);
  }
}

static void on_bg_method_call(FlMethodChannel* channel,
                              FlMethodCall* method_call,
                              gpointer user_data) {
  const gchar* method = fl_method_call_get_name(method_call);

  if (strcmp(method, "create") == 0) {
    handle_bg_create(method_call);
  } else if (strcmp(method, "evaluateJavascript") == 0) {
    handle_bg_evaluate_javascript(method_call);
  } else if (strcmp(method, "addJavaScriptHandler") == 0) {
    handle_bg_add_js_handler(method_call);
  } else if (strcmp(method, "sendKey") == 0) {
    handle_bg_send_key(method_call);
  } else if (strcmp(method, "destroy") == 0) {
    handle_bg_destroy(method_call);
  } else {
    g_autoptr(FlMethodResponse) response =
        FL_METHOD_RESPONSE(fl_method_not_implemented_response_new());
    fl_method_call_respond(method_call, response, nullptr);
  }
}

// --- Event channel handlers ---
static FlMethodErrorResponse* on_event_listen(FlEventChannel* channel,
                                              FlValue* args,
                                              gpointer user_data) {
  g_listening = TRUE;
  return nullptr;
}

static FlMethodErrorResponse* on_event_cancel(FlEventChannel* channel,
                                              FlValue* args,
                                              gpointer user_data) {
  g_listening = FALSE;
  return nullptr;
}

static FlMethodErrorResponse* on_bg_event_listen(FlEventChannel* channel,
                                                  FlValue* args,
                                                  gpointer user_data) {
  g_bg_listening = TRUE;
  return nullptr;
}

static FlMethodErrorResponse* on_bg_event_cancel(FlEventChannel* channel,
                                                  FlValue* args,
                                                  gpointer user_data) {
  g_bg_listening = FALSE;
  return nullptr;
}

// --- Public API ---
void webview_bridge_init(FlBinaryMessenger* messenger, GtkOverlay* overlay) {
  g_overlay = overlay;

  // Main webview: method channel for commands (Dart -> native).
  g_autoptr(FlStandardMethodCodec) codec = fl_standard_method_codec_new();
  g_method_channel = fl_method_channel_new(messenger, "frank_client/webview",
                                           FL_METHOD_CODEC(codec));
  fl_method_channel_set_method_call_handler(g_method_channel, on_method_call,
                                            nullptr, nullptr);

  // Main webview: event channel for events (native -> Dart).
  g_autoptr(FlStandardMethodCodec) event_codec =
      fl_standard_method_codec_new();
  g_event_channel = fl_event_channel_new(
      messenger, "frank_client/webview_events", FL_METHOD_CODEC(event_codec));
  fl_event_channel_set_stream_handlers(g_event_channel, on_event_listen,
                                       on_event_cancel, nullptr, nullptr);

  // Background webview: method channel.
  g_autoptr(FlStandardMethodCodec) bg_codec = fl_standard_method_codec_new();
  g_bg_method_channel = fl_method_channel_new(
      messenger, "frank_client/bg_webview", FL_METHOD_CODEC(bg_codec));
  fl_method_channel_set_method_call_handler(
      g_bg_method_channel, on_bg_method_call, nullptr, nullptr);

  // Background webview: event channel.
  g_autoptr(FlStandardMethodCodec) bg_event_codec =
      fl_standard_method_codec_new();
  g_bg_event_channel = fl_event_channel_new(
      messenger, "frank_client/bg_webview_events",
      FL_METHOD_CODEC(bg_event_codec));
  fl_event_channel_set_stream_handlers(g_bg_event_channel, on_bg_event_listen,
                                       on_bg_event_cancel, nullptr, nullptr);
}

void webview_bridge_dispose(void) {
  // Clean up main webview.
  if (g_webview_widget != nullptr && g_overlay != nullptr) {
    gtk_container_remove(GTK_CONTAINER(g_overlay), g_webview_widget);
  }
  g_webview = nullptr;
  g_webview_widget = nullptr;
  g_content_manager = nullptr;
  g_handler_signals.clear();
  g_listening = FALSE;

  // Clean up background webview.
  if (g_bg_webview_widget != nullptr && g_overlay != nullptr) {
    gtk_container_remove(GTK_CONTAINER(g_overlay), g_bg_webview_widget);
  }
  g_bg_webview = nullptr;
  g_bg_webview_widget = nullptr;
  g_bg_content_manager = nullptr;
  g_bg_handler_signals.clear();
  g_bg_listening = FALSE;

  // Free shared web context.
  if (g_web_context != nullptr) {
    g_object_unref(g_web_context);
    g_web_context = nullptr;
  }
  g_overlay = nullptr;

  g_clear_object(&g_method_channel);
  g_clear_object(&g_event_channel);
  g_clear_object(&g_bg_method_channel);
  g_clear_object(&g_bg_event_channel);
}
