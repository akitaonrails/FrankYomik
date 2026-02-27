#include "webview_bridge.h"

#include <cstring>
#include <map>
#include <string>

#include <webkit2/webkit2.h>

// --- Globals ---
static GtkOverlay* g_overlay = nullptr;
static WebKitWebView* g_webview = nullptr;
static GtkWidget* g_webview_widget = nullptr;
static FlMethodChannel* g_method_channel = nullptr;
static FlEventChannel* g_event_channel = nullptr;
static gboolean g_listening = FALSE;
static WebKitUserContentManager* g_content_manager = nullptr;

// Track registered JS handler names so we can unregister them.
static std::map<std::string, gulong> g_handler_signals;

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

// --- Forward declarations ---
static void on_method_call(FlMethodChannel* channel, FlMethodCall* method_call,
                           gpointer user_data);

// --- Helper: send event to Dart ---
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

// --- WebKitWebView signal handlers ---
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

// --- JS message handler callback ---
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

// --- Method channel: create WebView ---
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

    // Inject the JS bridge shim on every page (document start).
    WebKitUserScript* script = webkit_user_script_new(
        JS_BRIDGE_SHIM, WEBKIT_USER_CONTENT_INJECT_ALL_FRAMES,
        WEBKIT_USER_SCRIPT_INJECT_AT_DOCUMENT_START, nullptr, nullptr);
    webkit_user_content_manager_add_script(g_content_manager, script);
    webkit_user_script_unref(script);

    g_webview = WEBKIT_WEB_VIEW(
        webkit_web_view_new_with_user_content_manager(g_content_manager));
    g_webview_widget = GTK_WIDGET(g_webview);

    g_signal_connect(g_webview, "load-changed",
                     G_CALLBACK(on_load_changed), nullptr);
    g_signal_connect(g_webview, "notify::uri",
                     G_CALLBACK(on_uri_changed), nullptr);

    gtk_overlay_add_overlay(g_overlay, g_webview_widget);
  }

  // Apply user agent if provided.
  if (user_agent != nullptr) {
    WebKitSettings* settings = webkit_web_view_get_settings(g_webview);
    webkit_settings_set_user_agent(settings, user_agent);
  }

  // Enable JavaScript.
  WebKitSettings* settings = webkit_web_view_get_settings(g_webview);
  webkit_settings_set_enable_javascript(settings, TRUE);

  webkit_web_view_load_uri(g_webview, url);
  gtk_widget_show(g_webview_widget);

  g_autoptr(FlMethodResponse) response =
      FL_METHOD_RESPONSE(fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

// --- Method channel: setFrame ---
static void handle_set_frame(FlMethodCall* method_call) {
  // Not used for overlay positioning in GTK — the WebView fills the overlay.
  g_autoptr(FlMethodResponse) response =
      FL_METHOD_RESPONSE(fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

// --- Method channel: evaluateJavascript ---
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

// --- Method channel: addJavaScriptHandler ---
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

// --- Method channel: destroy ---
static void handle_destroy(FlMethodCall* method_call) {
  if (g_webview_widget != nullptr) {
    gtk_widget_hide(g_webview_widget);
    gtk_container_remove(GTK_CONTAINER(g_overlay), g_webview_widget);
    g_webview = nullptr;
    g_webview_widget = nullptr;
    g_content_manager = nullptr;
    g_handler_signals.clear();
  }

  g_autoptr(FlMethodResponse) response =
      FL_METHOD_RESPONSE(fl_method_success_response_new(fl_value_new_bool(TRUE)));
  fl_method_call_respond(method_call, response, nullptr);
}

// --- Method channel dispatcher ---
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

// --- Public API ---
void webview_bridge_init(FlBinaryMessenger* messenger, GtkOverlay* overlay) {
  g_overlay = overlay;

  // Method channel for commands (Dart -> native).
  g_autoptr(FlStandardMethodCodec) codec = fl_standard_method_codec_new();
  g_method_channel = fl_method_channel_new(messenger, "frank_client/webview",
                                           FL_METHOD_CODEC(codec));
  fl_method_channel_set_method_call_handler(g_method_channel, on_method_call,
                                            nullptr, nullptr);

  // Event channel for events (native -> Dart).
  g_autoptr(FlStandardMethodCodec) event_codec =
      fl_standard_method_codec_new();
  g_event_channel = fl_event_channel_new(
      messenger, "frank_client/webview_events", FL_METHOD_CODEC(event_codec));
  fl_event_channel_set_stream_handlers(g_event_channel, on_event_listen,
                                       on_event_cancel, nullptr, nullptr);
}

void webview_bridge_dispose(void) {
  if (g_webview_widget != nullptr && g_overlay != nullptr) {
    gtk_container_remove(GTK_CONTAINER(g_overlay), g_webview_widget);
  }
  g_webview = nullptr;
  g_webview_widget = nullptr;
  g_content_manager = nullptr;
  g_overlay = nullptr;
  g_handler_signals.clear();
  g_listening = FALSE;

  g_clear_object(&g_method_channel);
  g_clear_object(&g_event_channel);
}
