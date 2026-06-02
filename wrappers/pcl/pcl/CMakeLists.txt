cmake_minimum_required(VERSION 3.10)

project(RealSensePCLExample)

file(GLOB PCL_IMGUI_SOURCES "../../../third-party/imgui/*.cpp")

add_executable(rs-pcl rs-pcl.cpp ${PCL_IMGUI_SOURCES})
set_property(TARGET rs-pcl PROPERTY CXX_STANDARD 11)
target_link_libraries(rs-pcl ${DEPENDENCIES})
target_include_directories(rs-pcl PRIVATE ../../../third-party/imgui ../../../examples)
if(NOT glfw3_DIR)
    target_include_directories(rs-pcl PUBLIC "${PROJECT_SOURCE_DIR}/third-party/glfw/include")
    target_link_directories(rs-pcl PUBLIC "${CMAKE_BINARY_DIR}/third-party/glfw/src/${CMAKE_BUILD_TYPE}")
endif()
set_target_properties (rs-pcl PROPERTIES
    FOLDER "Examples/PCL"
)

foreach(flag_var
        CMAKE_CXX_FLAGS CMAKE_CXX_FLAGS_DEBUG CMAKE_CXX_FLAGS_RELEASE
        CMAKE_CXX_FLAGS_MINSIZEREL CMAKE_CXX_FLAGS_RELWITHDEBINFO
        CMAKE_C_FLAGS CMAKE_C_FLAGS_DEBUG CMAKE_C_FLAGS_RELEASE
        CMAKE_C_FLAGS_MINSIZEREL CMAKE_C_FLAGS_RELWITHDEBINFO)
    if(${flag_var} MATCHES "/MT")
        string(REGEX REPLACE "/MT" "/MD" ${flag_var} "${${flag_var}}")
    endif(${flag_var} MATCHES "/MT")
endforeach(flag_var)

install(
    TARGETS

    rs-pcl

    RUNTIME DESTINATION
    ${CMAKE_INSTALL_PREFIX}/bin
)
