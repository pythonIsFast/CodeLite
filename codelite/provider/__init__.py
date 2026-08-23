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

"""ChatGPT-OAuth-to-OpenAI-API provider layer.

Exposes small, in-process functions (``send_chat``, ``send_responses``,
``generate_image``, ``edit_image``, ``list_models``) that a future agent
loop can call directly, plus an optional local HTTP proxy
(:mod:`codelite.provider.server`) that exposes the same functionality as an
OpenAI-compatible API for existing OpenAI-client tooling.
"""

from .config import ProviderConfig
from .session import Session, load_session

__all__ = ["ProviderConfig", "Session", "load_session"]
