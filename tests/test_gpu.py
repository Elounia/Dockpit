import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dockpit import gpu


class GpuTests(unittest.TestCase):
    def test_intel_shared_memory_name_and_clocks_are_not_utilization(self):
        with tempfile.TemporaryDirectory() as root, patch.object(gpu.shutil, 'which', return_value=None):
            card = Path(root) / 'sys/class/drm/card1'
            device = card / 'device'
            device.mkdir(parents=True)
            (device / 'vendor').write_text('0x8086')
            (device / 'device').write_text('0x4692')
            (card / 'gt_act_freq_mhz').write_text('0')
            (card / 'gt_max_freq_mhz').write_text('1450')
            database = Path(root) / 'usr/share/hwdata/pci.ids'
            database.parent.mkdir(parents=True)
            database.write_text('1002  AMD\n\t4692  Wrong vendor\n8086  Intel Corporation\n\t4692  Alder Lake-S GT1 [UHD Graphics 730]\n\t\t8086 0000  Subsystem\n')
            result = gpu.get_gpu_metrics(root)
        intel = result['devices'][0]
        self.assertEqual(intel['name'], 'Intel Alder Lake-S GT1 [UHD Graphics 730]')
        self.assertEqual(intel['memory_kind'], 'shared')
        self.assertEqual(intel['frequency_mhz'], 0)
        self.assertEqual(intel['max_frequency_mhz'], 1450)
        self.assertIsNone(result['utilization_pct'])
        self.assertIsNone(result['memory_total_bytes'])
        self.assertIn('not configured', intel['utilization_note'])

    def test_intel_unknown_device_does_not_assume_shared_memory(self):
        with tempfile.TemporaryDirectory() as root, patch.object(gpu.shutil, 'which', return_value=None):
            device = Path(root) / 'sys/class/drm/card0/device'
            device.mkdir(parents=True)
            (device / 'vendor').write_text('0x8086')
            (device / 'device').write_text('0xffff')
            row = gpu.get_gpu_metrics(root)['devices'][0]
        self.assertEqual(row['name'], 'Intel 0xffff')
        self.assertEqual(row['memory_kind'], 'unknown')
        self.assertIsNone(row['frequency_mhz'])
        self.assertNotIn('memory_note', row)

    def test_intel_discrete_gpu_and_per_gt_clock_fallback(self):
        with tempfile.TemporaryDirectory() as root, patch.object(gpu.shutil, 'which', return_value=None):
            card = Path(root) / 'sys/class/drm/card0'
            device = card / 'device'
            device.mkdir(parents=True)
            (device / 'vendor').write_text('0x8086')
            (device / 'product_name').write_text('Intel Arc A770')
            for index, frequency in enumerate(('300', '1200', 'invalid')):
                gt = card / f'gt/gt{index}'
                gt.mkdir(parents=True)
                (gt / 'rps_act_freq_mhz').write_text(frequency)
            row = gpu.get_gpu_metrics(root)['devices'][0]
        self.assertEqual(row['memory_kind'], 'unknown')
        self.assertEqual(row['frequency_mhz'], 1200)
        self.assertIsNone(row['max_frequency_mhz'])
        self.assertIsNone(row['utilization_pct'])

    def test_nvidia_multiple_gpus_and_unsupported_values(self):
        output = '0, GPU-a, "NVIDIA, Test", 40, 100, 1000, 55, 90\n1, GPU-b, Second, N/A, 200, 1000, N/A, N/A\n'
        with tempfile.TemporaryDirectory() as root, patch.object(gpu.shutil, 'which', return_value='/bin/nvidia-smi'), patch.object(gpu, 'nvidia', return_value=output):
            result = gpu.get_gpu_metrics(root)
        self.assertEqual(result['devices'][0]['name'], 'NVIDIA, Test')
        self.assertEqual(result['memory_used_bytes'], 300 * 1024**2)
        self.assertIsNone(result['utilization_pct'])
        self.assertIsNone(result['devices'][1]['temperature_c'])

    def test_drm_amd_and_no_hardware(self):
        with tempfile.TemporaryDirectory() as root, patch.object(gpu.shutil, 'which', return_value=None):
            self.assertEqual(gpu.get_gpu_metrics(root)['devices'], [])
            device = Path(root) / 'sys/class/drm/card0/device'
            device.mkdir(parents=True)
            for key, value in {'vendor': '0x1002', 'gpu_busy_percent': '37', 'mem_info_vram_used': '1024', 'mem_info_vram_total': '4096'}.items():
                (device / key).write_text(value)
            result = gpu.get_gpu_metrics(root)
        self.assertEqual(result['utilization_pct'], 37)
        self.assertEqual(result['memory_used_bytes'], 1024)

    def test_process_mapping_systemd_cgroup_and_multiple_gpus(self):
        cid = 'a' * 64
        rows = [dict(full_id=cid, state='running'), dict(full_id='b' * 64, state='running'), dict(full_id='c' * 64, state='exited')]
        output = '# gpu pid type sm mem enc dec fb command\n# Idx # C/G % % % % MB name\n0 123 C 30 1 0 0 128 worker\n1 123 C 40 1 0 0 256 worker\n'
        with tempfile.TemporaryDirectory() as proc, patch.object(gpu, 'nvidia', return_value=output):
            path = Path(proc) / '123'
            path.mkdir()
            (path / 'cgroup').write_text(f'0::/system.slice/docker-{cid}.scope\n')
            gpu.attach_container_gpu(rows, {'devices': [{'vendor': 'NVIDIA'}]}, proc)
        self.assertEqual(rows[0]['gpu_perc'], 70)
        self.assertEqual(rows[0]['gpu_memory_bytes'], 384 * 1024**2)
        self.assertEqual(rows[1]['gpu_perc'], 0)
        self.assertIsNone(rows[2]['gpu_perc'])

    def test_missing_permissions_or_unsupported_counters_stay_unknown(self):
        rows = [dict(full_id='a' * 64, state='running')]
        output = '# gpu pid type sm mem enc dec fb command\n0 123 C - - - - - worker\n'
        with tempfile.TemporaryDirectory() as proc, patch.object(gpu, 'nvidia', return_value=output):
            gpu.attach_container_gpu(rows, {'devices': [{'vendor': 'NVIDIA'}]}, proc)
        self.assertIsNone(rows[0]['gpu_perc'])
        self.assertIsNone(rows[0]['gpu_memory_bytes'])
        self.assertFalse(rows[0]['gpu_stats_available'])
        with patch.object(gpu, 'nvidia', side_effect=subprocess.TimeoutExpired('nvidia-smi', 4)):
            gpu.attach_container_gpu(rows, {'devices': [{'vendor': 'NVIDIA'}]}, '/proc')
        self.assertIsNone(rows[0]['gpu_perc'])

    def test_unsupported_sm_preserves_vram(self):
        cid = 'a' * 64
        rows = [dict(full_id=cid, state='running')]
        with tempfile.TemporaryDirectory() as proc, patch.object(gpu, 'nvidia', return_value='# gpu pid type sm fb command\n0 123 C - 256 worker\n'):
            path = Path(proc) / '123'
            path.mkdir()
            (path / 'cgroup').write_text(f'0::/docker/{cid}')
            gpu.attach_container_gpu(rows, {'devices': [{'vendor': 'NVIDIA'}]}, proc)
        self.assertIsNone(rows[0]['gpu_perc'])
        self.assertEqual(rows[0]['gpu_memory_bytes'], 256 * 1024**2)
