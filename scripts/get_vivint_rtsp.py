#!/usr/bin/env python3
"""
get_vivint_rtsp.py - Retrieve direct RTSP stream URLs for Vivint cameras
Requires: pip install vivintpy
Usage: python scripts/get_vivint_rtsp.py <vivint_username_or_email> <vivint_password>
"""

import asyncio
import sys

try:
    from vivintpy.account import Account
    from vivintpy.devices.camera import Camera
    from vivintpy.exceptions import VivintSkyApiMfaRequiredError, VivintSkyApiAuthenticationError
except Exception as err:
    print(f"\nImport Error: {err}")
    print("Please ensure vivintpy is installed using: pip install vivintpy\n")
    sys.exit(1)


async def main():
    if len(sys.argv) < 3:
        print("\nUsage: python scripts/get_vivint_rtsp.py <vivint_username> <vivint_password>")
        sys.exit(1)

    username = sys.argv[1]
    password = sys.argv[2]

    print(f"Connecting to Vivint account for {username}...")
    account = Account(username=username, password=password)

    try:
        await account.connect(load_devices=True)
    except VivintSkyApiMfaRequiredError:
        mfa_code = input("\nVivint 2FA required. Enter the MFA code received: ").strip()
        await account.verify_mfa(mfa_code)
        print("MFA verification successful!")
    except VivintSkyApiAuthenticationError as e:
        print(f"\nAuthentication failed: {e}")
        return
    except Exception as e:
        print(f"\nConnection error: {e}")
        return

    print("\n" + "=" * 60)
    print(" VIVINT CAMERA RTSP STREAMS FOUND")
    print("=" * 60)

    camera_found = False
    for system in account.systems:
        for alarm_panel in system.alarm_panels:
            for device in alarm_panel.devices:
                if isinstance(device, Camera):
                    camera_found = True
                    print(f"\n[Camera: {device.name}] (ID: {device.id})")
                    try:
                        internal_hd = await device.get_rtsp_url(internal=True, hd=True)
                        print(f"  Internal HD (Recording):     {internal_hd}")
                    except Exception as e:
                        print(f"  Internal HD error: {e}")

                    try:
                        internal_sd = await device.get_rtsp_url(internal=True, hd=False)
                        print(f"  Internal Non-HD (Detection): {internal_sd}")
                    except Exception as e:
                        print(f"  Internal Non-HD error: {e}")

                    try:
                        external_hd = await device.get_rtsp_url(internal=False, hd=True)
                        print(f"  External HD:                 {external_hd}")
                    except Exception as e:
                        print(f"  External HD error: {e}")

    if not camera_found:
        print("\nNo camera devices found associated with this account.")

    print("\n" + "=" * 60)
    await account.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
