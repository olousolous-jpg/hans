cmake_minimum_required(VERSION 3.20)
project(kwin-effect-liquidglass VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(ECM 6.0 REQUIRED NO_MODULE)
list(APPEND CMAKE_MODULE_PATH ${ECM_MODULE_PATH})

include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)

find_package(Qt6 6.8 REQUIRED COMPONENTS Core Gui Widgets Quick DBus)
find_package(KF6 6.0 REQUIRED COMPONENTS Config CoreAddons)
find_package(KWin 6.7 REQUIRED)
find_package(KDecoration3 REQUIRED)
# kód pro X11 okna (Xwayland) volá xcb přímo
find_package(XCB COMPONENTS XCB)

set(liquidglass_SOURCES
    src/main.cpp
    src/liquidglass.cpp
    src/liquidglass.qrc
)

kconfig_add_kcfg_files(liquidglass_SOURCES src/liquidglassconfig.kcfgc)

kcoreaddons_add_plugin(liquidglass
    SOURCES ${liquidglass_SOURCES}
    INSTALL_NAMESPACE "kwin/effects/plugins"
)

# továrna efektu je schovaná v makru KWinu, automoc o něm musí vědět
set_property(TARGET liquidglass APPEND PROPERTY AUTOMOC_MACRO_NAMES "KWIN_EFFECT_FACTORY_SUPPORTED_ENABLED")

target_include_directories(liquidglass PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(liquidglass PRIVATE
    KWin::kwin
    KF6::ConfigGui
    KF6::CoreAddons
    KDecoration3::KDecoration
)
if(TARGET XCB::XCB)
    target_link_libraries(liquidglass PRIVATE XCB::XCB)
endif()
