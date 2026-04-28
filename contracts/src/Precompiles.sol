// Copyright 2026 Circle Internet Group, Inc. All rights reserved.
//
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

pragma solidity ^0.8.29;

/// @title Precompiles
/// @notice Genesis-fixed addresses of Arc precompiles.
library Precompiles {
    address internal constant NATIVE_COIN_AUTHORITY = 0x1800000000000000000000000000000000000000;
    address internal constant NATIVE_COIN_CONTROL = 0x1800000000000000000000000000000000000001;
    address internal constant SYSTEM_ACCOUNTING = 0x1800000000000000000000000000000000000002;
    address internal constant CALL_FROM = 0x1800000000000000000000000000000000000003;
    address internal constant PQ = 0x1800000000000000000000000000000000000004;
}
