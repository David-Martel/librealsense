// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
#pragma once


// RSUTILS_LOCAL marks a global object as private to the module that defines it.
//
// rsutils is a STATIC library that is linked PUBLIC into the shared realsense2 (see the top-level
// CMakeLists.txt), so every executable that links realsense2 also links librsutils.a and ends up
// with a SECOND definition of each global rsutils owns. With default (preemptible) visibility ELF
// resolves both the library's and the executable's references to the executable's copy, so
// librealsense2.so's initializer constructs the executable's object and registers a destructor for
// it -- and the executable's initializer does the same. The object is then constructed twice and
// destroyed twice: for anything holding a heap allocation that is an exit-time double free
// (SIGABRT) in every tool that links both.
//
// Hidden visibility gives each module its own private, non-preemptible copy, so each is constructed
// and destroyed exactly once. It is only correct for globals that are immutable and compared by
// VALUE rather than by address -- two modules must never need to agree on which object they hold.
//
// The crash is latent: it surfaces only when the linker happens to pull the defining archive member
// into the executable, which depends on the language standard and on which other rsutils members
// are referenced. It was observed at -std=c++20 against nlohmann 3.12 while -std=c++14 silently
// avoided it. See docs/gb10/UPGRADE-PLAN-2026-09-10.md section 11.
//
// The attribute must be repeated on the definition -- GCC emits default visibility for a definition
// that does not carry it, even when the declaration did.

#if defined( _WIN32 )
#define RSUTILS_LOCAL  // PE has no symbol interposition; the DLL exports only what src/realsense.def lists
#else
#define RSUTILS_LOCAL __attribute__( ( visibility( "hidden" ) ) )
#endif
