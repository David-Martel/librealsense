cmake_minimum_required(VERSION 3.10)

project(RealSenseKinfuExample)

find_package(glfw3 3.3 REQUIRED)
find_package(OpenGL REQUIRED)

set(DEPENDENCIES glfw ${OPENGL_LIBRARIES} ${DEPENDENCIES})

file(GLOB KINFU_IMGUI_SOURCES "../../../third-party/imgui/*.cpp")

add_executable(rs-kinfu rs-kinfu.cpp ../../../examples/example.hpp ${KINFU_IMGUI_SOURCES})
set_property(TARGET rs-kinfu PROPERTY CXX_STANDARD 11)
target_link_libraries(rs-kinfu ${DEPENDENCIES})
include_directories(../../../examples ../../../third-party/imgui)
set_target_properties (rs-kinfu PROPERTIES
	FOLDER "Examples/OpenCV"
)

install(
	TARGETS

	rs-kinfu

	RUNTIME DESTINATION
	${CMAKE_INSTALL_PREFIX}/bin
)
