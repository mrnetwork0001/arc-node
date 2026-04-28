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

// Reads a Forge-compiled artifact. Use for any contract whose bytecode feeds
// into CREATE2 address computation or is compared against on-chain genesis
// code — Hardhat's own compile emits divergent bytecode (different source-path
// keys in metadata), so genesis-critical reads must come from Forge.

import fs from 'fs'
import path from 'path'
import type { Abi, Hex } from 'viem'

export type ForgeArtifact = {
  abi: Abi
  bytecode: Hex
  deployedBytecode: Hex
}

const FORGE_OUT_DIR = path.resolve(__dirname, '../../contracts/out/forge')

type ForgeRawArtifact = {
  abi: Abi
  bytecode: { object: Hex }
  deployedBytecode: { object: Hex }
}

export function readForgeArtifactSync(contractName: string, sourceFile?: string): ForgeArtifact {
  const file = sourceFile ?? `${contractName}.sol`
  const artifactPath = path.join(FORGE_OUT_DIR, file, `${contractName}.json`)
  const raw = JSON.parse(fs.readFileSync(artifactPath, 'utf8')) as ForgeRawArtifact
  return {
    abi: raw.abi,
    bytecode: raw.bytecode.object,
    deployedBytecode: raw.deployedBytecode.object,
  }
}
