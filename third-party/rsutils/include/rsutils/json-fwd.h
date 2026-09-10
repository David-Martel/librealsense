// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2023 RealSense, Inc. All Rights Reserved.
#pragma once

// Turn off normal JSON I/O (operator<<) serialization
// This disables a few things like json::parse, but we do it because of conflict between our operator<< and the built-in
// one by json. Otherwise (if we do not need custom stream serialization) it's not needed...
#define JSON_NO_IO

#include <nlohmann/json_fwd.hpp>


namespace rsutils {


using json_key = std::string;  // default of basic_json


class json_ref;   // Our 'nested' json const reference wrapper
class json_base;  // The 'json' base class


// We use a custom base class to inject our own functionality into every 'json' object:
//     json <- nlohmann::basic_json<...> <- json_base
// 
// NOTE: everything here is templated or forward-declared: neither 'json' nor 'json_ref' are defined yet, and 'json'
// can't even be referred to because there's a circular dependency (it derives from json_base).
// 
// This class defines new functionality that the basic 'json' doesn't have that we want (like nested()), but the actual
// implementations are always in 'json_ref'!
// 
// WARNING: we cannot override already-existing 'json' functions! In fact, it's the other way around: 'json' functions
// will override ours, so, for example, if we defined find() it would not be usable.
//
class json_base
{
    // Since we know how we're derived from, we can get the json, or the reference to it, from this:
    inline json_ref _ref() const;

public:
    inline bool exists() const;
    inline operator bool() const;

    template< class T > inline T default_value( T const & default_value ) const;

    template< class T > inline bool get_ex( T & value ) const;

    inline json_ref default_object() const;
    inline json_ref default_string() const;

    inline std::string const & string_ref() const;
    inline std::string const & string_ref_or_empty() const;

    template< typename... Rest > inline json_ref nested( Rest... rest ) const;

    // Recursively patches with contents of 'overrides', which must be a JSON object
    void override( json_ref overrides, std::string const & what = {} );
};


using json = nlohmann::basic_json< std::map,  // all template arguments are defaults
                                   std::vector,
                                   json_key,
                                   bool,
                                   std::int64_t,
                                   std::uint64_t,
                                   double,
                                   std::allocator,
                                   nlohmann::adl_serializer,
                                   std::vector< std::uint8_t >,
                                   json_base >;  // except custom base class!


// We can't put these inside json, unfortunately...
//
// These four are defined in json.cpp, which lives in rsutils -- a STATIC library that is linked
// PUBLIC into the shared realsense2 (see the top-level CMakeLists.txt). A static library linked
// into both a shared object and the executables that load it produces TWO definitions of every
// global it owns. With default (preemptible) visibility, ELF resolves both to the executable's
// copy, so librealsense2.so's initializer constructs the executable's object and registers a
// destructor for it -- and the executable does the same. The object is then constructed twice and
// destroyed twice: an exit-time double free (SIGABRT) in every tool that links both.
//
// Hidden visibility makes each module's copy private and non-preemptible, so each is constructed
// and destroyed exactly once. That is safe here because these are immutable sentinels compared by
// VALUE, never by address: "missing" is detected as _j.is_discarded() in json_ref::exists(), not
// as &_j == &missing_json.
//
// Without this, the crash is latent -- it only surfaces when the linker happens to pull json.cpp's
// archive member into the executable, which depends on the language standard and on which other
// rsutils members are referenced. It was observed at -std=c++20 against nlohmann 3.12 while
// -std=c++14 silently avoided it. See docs/gb10/UPGRADE-PLAN-2026-09-10.md section 11.
#if defined( _WIN32 )
#define RSUTILS_SENTINEL  // exports are controlled by the .def file
#else
#define RSUTILS_SENTINEL __attribute__( ( visibility( "hidden" ) ) )
#endif

extern json const RSUTILS_SENTINEL null_json;     // default json state
extern json const RSUTILS_SENTINEL missing_json;  // i.e., not there: exists() will be 'false'
extern json const RSUTILS_SENTINEL empty_json_string;
extern json const RSUTILS_SENTINEL empty_json_object;


std::ostream & operator<<( std::ostream &, const json & );



}  // namespace rsutils
