# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A hidden, scriptable browser for dynamic/JS-heavy pages.

``web_search``/``web_fetch`` stay the default for reading the web -- they are
cheap and need no window. This package is for the pages those cannot handle:
ones that render their content with JavaScript. It reuses the same system
webview :mod:`codelite.app.window` already opens for the UI (WebKitGTK on
Linux, WebView2 on Windows), so it adds no new heavy dependency -- just a
second, invisible window driven by the agent instead of by the user.

``host.py`` runs as its own child process (see :func:`codelite.browser.client
.BrowserClient`) rather than a second window in the app's own process: pywebview
blocks its owning thread for as long as its event loop runs, and a page that
hangs or crashes the renderer must not be able to take the whole app down with
it.
"""
