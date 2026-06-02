cmake_minimum_required (VERSION 3.10)
project (rs2driver)

# DEPS
set(OPENNI2_DIR "c:/Program Files/OpenNI2" CACHE FILEPATH "OpenNI2 SDK directory")
set(REALSENSE2_DIR "c:/Program Files (x86)/Intel RealSense SDK 2.0" CACHE FILEPATH "RealSense2 SDK directory")

if (UNIX)
    find_path(OPENNI2_INCLUDE_DIR
        NAMES Driver/OniDriverAPI.h
        PATHS
            /usr/include/openni2
            /usr/local/include/openni2
            ${OPENNI2_DIR}
    )
    if (NOT OPENNI2_INCLUDE_DIR)
        message(FATAL_ERROR "OpenNI2 driver headers were not found")
    endif()
endif()

# INCLUDE DIR
if (UNIX)
    include_directories (${OPENNI2_INCLUDE_DIR})
else ()
    include_directories (${OPENNI2_DIR}/Include)
endif ()

include_directories (${REALSENSE2_DIR}/include)
include_directories (src)

# LINK DIR
if (CMAKE_SIZEOF_VOID_P EQUAL 8)
    link_directories (${REALSENSE2_DIR}/lib/x64)
elseif (CMAKE_SIZEOF_VOID_P EQUAL 4)
    link_directories (${REALSENSE2_DIR}/lib/x86)
endif ()

# OUTPUT DIR
set (CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/_out)
set (CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/_out)
set (CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/_out)

# SOURCE FILES
set (INCLUDE_FILES
	src/Rs2Base.h
	src/Rs2Stream.h
	src/Rs2Device.h
	src/Rs2Driver.h
	src/Profiler.h
	src/Profiler.inl
)
set (SRC_FILES
	src/Rs2Base.cpp
	src/Rs2Stream.cpp
	src/Rs2StreamProps.cpp
	src/Rs2Device.cpp
	src/Rs2DeviceProps.cpp
	src/Rs2Driver.cpp
)

# FLAGS
if(MSVC)
	set (CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} /W4")
	set (CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /Oi /Ot /GL /GF /MD /GS- /Gy /fp:fast /arch:AVX2")
	set (CMAKE_SHARED_LINKER_FLAGS "${CMAKE_SHARED_LINKER_FLAGS} /SUBSYSTEM:WINDOWS")
endif()

# LINK
add_library (rs2driver SHARED ${INCLUDE_FILES} ${SRC_FILES})
target_link_libraries (rs2driver realsense2)
