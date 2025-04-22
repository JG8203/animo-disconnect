import argparse
import shutil
from DrissionPage import ChromiumOptions, Chromium

URL = 'https://animo.sys.dlsu.edu.ph/psp/ps/?cmd=login&languageCd=ENG'
BASE_PORT = 9333

def spawn_instances(total: int, base_port: int = BASE_PORT) -> None:
    for idx in range(total):
        port = base_port + idx
        data_path = f'data_{idx}'

        co = ChromiumOptions().set_local_port(port).set_user_data_path(data_path)
        br = Chromium(co)
        tab = br.new_tab(url=URL)
        tab.wait.doc_loaded()

        if tab.ele('xpath://html/body/table/tbody/tr[1]/td/img', timeout=15):
            print(f'[#-{idx}]', tab.cookies().as_str())

        br.quit()
        shutil.rmtree(data_path, ignore_errors=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Spawn isolated DrissionPage browsers and dump cookies.')
    parser.add_argument('-n', '--number', type=int, default=9,
                        help='How many Chromium instances to launch')
    args = parser.parse_args()
    spawn_instances(args.number)
