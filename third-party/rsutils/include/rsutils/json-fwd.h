// License: Apache 2.0. See LICENSE file in root directory.
// Copyright(c) 2023 RealSense, Inc. All Rights Reserved.
#pragma once

#include <rsutils/visibility.h>

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
// RSUTILS_LOCAL (see rsutils/visibility.h) keeps each module's copy of these private: rsutils is a
// static library linked into both librealsense2 and every executable that loads it, and with
// default visibility the duplicate definitions collapse onto one object that is then constructed
// and destroyed twice -- an exit-time double free. It is safe here because these are immutable
// sentinels compared by VALUE, never by address: "missing" is detected as _j.is_discarded() in
// json_ref::exists(), not as &_j == &missing_json.
extern json const RSUTILS_LOCAL null_json;     // default json state
extern json const RSUTILS_LOCAL missing_json;  // i.e., not there: exists() will be 'false'
extern json const RSUTILS_LOCAL empty_json_string;
extern json const RSUTILS_LOCAL empty_json_object;


std::ostream & operator<<( std::ostream &, const json & );



}  // namespace rsutils
