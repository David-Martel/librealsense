# License: Apache 2.0. See LICENSE file in root directory.

cmake_minimum_required(VERSION 3.10)

project(rs-gb10-profiler)

if(BUILD_GRAPHICAL_EXAMPLES AND NOT APPLE)

add_executable(${PROJECT_NAME} rs-gb10-profiler.cpp ${IMGUI_SOURCES})
set_property(TARGET ${PROJECT_NAME} PROPERTY CXX_STANDARD 20)
set_property(TARGET ${PROJECT_NAME} PROPERTY CXX_STANDARD_REQUIRED ON)
set_property(TARGET ${PROJECT_NAME} PROPERTY CXX_EXTENSIONS OFF)
target_link_libraries(${PROJECT_NAME} ${DEPENDENCIES} realsense2-gl)
target_include_directories(${PROJECT_NAME} PRIVATE ../../examples ../../third-party/imgui)
target_compile_definitions(${PROJECT_NAME} PRIVATE NO_ELPP_INIT)
set_target_properties(${PROJECT_NAME} PROPERTIES FOLDER Tools)
set_target_properties(${PROJECT_NAME} PROPERTIES INSTALL_RPATH "\$ORIGIN/../lib")
using_easyloggingpp(${PROJECT_NAME} SHARED)
install(TARGETS ${PROJECT_NAME} RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

endif()
