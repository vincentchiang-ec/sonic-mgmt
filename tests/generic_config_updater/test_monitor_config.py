import logging
import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.gu_utils import apply_patch, expect_op_success, expect_res_success
from tests.common.gu_utils import generate_tmpfile, delete_tmpfile
from tests.common.gu_utils import format_json_patch_for_multiasic
from tests.common.gu_utils import create_checkpoint, delete_checkpoint, rollback, rollback_or_reload

pytestmark = [
    pytest.mark.topology('any'),
]

logger = logging.getLogger(__name__)

MONITOR_CONFIG_TEST_CP = "monitor_config_test"
MONITOR_CONFIG_INITIAL_CP = "monitor_config_initial"
MONITOR_CONFIG_ACL_TABLE = "EVERFLOW_DSCP"
MONITOR_CONFIG_ACL_RULE = "RULE_1"
MONITOR_CONFIG_MIRROR_SESSION = "mirror_session_dscp"
MONITOR_CONFIG_POLICER = "policer_dscp"


def is_policer_supported(duthost):
    """
    Return True if policer is supported in MIRROR_SESSION creation on this platform, otherwise return False.
    """
    platform = duthost.facts.get('platform', '')
    if platform.startswith("x86_64-arista_7060x6_64pe"):
        return False
    return True


@pytest.fixture(scope='module')
def get_valid_acl_ports(rand_selected_front_end_dut, enum_rand_one_frontend_asic_index):
    """ Get valid acl ports that could be added to ACL table
    valid ports refers to the portchannels and ports not belongs portchannel
    """
    asic_namespace = rand_selected_front_end_dut.get_namespace_from_asic_id(enum_rand_one_frontend_asic_index)

    def _get_valid_acl_ports():
        ports = set()
        portchannel_members = set()

        cfg_facts = rand_selected_front_end_dut.config_facts(
            host=rand_selected_front_end_dut.hostname,
            source="running",
            verbose=False,
            namespace=asic_namespace
            )['ansible_facts']
        portchannel_member_dict = cfg_facts.get('PORTCHANNEL_MEMBER', {})
        for po, po_members in list(portchannel_member_dict.items()):
            ports.add(po)
            for po_member in po_members:
                portchannel_members.add(po_member)

        port_dict = cfg_facts.get('PORT', {})
        for key in port_dict:
            if key not in portchannel_members:
                port_role = cfg_facts['PORT'][key].get('role')
                if port_role and port_role != 'Ext':    # ensure port is front-panel port
                    continue
                ports.add(key)
        return list(ports)

    return _get_valid_acl_ports()


def bgp_monitor_config_cleanup(duthost, cli_namespace_prefix, namespace=None):
    """ Test requires no monitor config
    Clean up current monitor config if existed
    """
    cmds = []
    cmds.append('sonic-db-cli {} CONFIG_DB del "ACL_TABLE|{}"'
                .format(cli_namespace_prefix, MONITOR_CONFIG_ACL_TABLE))
    cmds.append('sonic-db-cli {} CONFIG_DB del "ACL_RULE|{}|{}"'
                .format(cli_namespace_prefix, MONITOR_CONFIG_ACL_TABLE, MONITOR_CONFIG_ACL_RULE))
    cmds.append('sonic-db-cli {} CONFIG_DB del "MIRROR_SESSION|{}"'
                .format(cli_namespace_prefix, MONITOR_CONFIG_MIRROR_SESSION))
    cmds.append('sonic-db-cli {} CONFIG_DB del "POLICER|{}"'
                .format(cli_namespace_prefix, MONITOR_CONFIG_POLICER))

    output = duthost.shell_cmds(cmds=cmds)['results']
    for res in output:
        pytest_assert(not res['rc'], "bgp monitor config cleanup failed.")


@pytest.fixture(autouse=True)
def setup_env(duthosts, rand_one_dut_front_end_hostname):
    """
    Setup/teardown fixture for syslog config

    Args:
        duthosts: list of DUTs.
        rand_selected_front_end_dut: The fixture returns a randomly selected DuT.
    """
    duthost = duthosts[rand_one_dut_front_end_hostname]
    create_checkpoint(duthost)

    yield

    try:
        logger.info("Rolled back to original checkpoint")
        rollback_or_reload(duthost)

    finally:
        delete_checkpoint(duthost)


def verify_monitor_config(duthost, ip_netns_namespace_prefix):
    """
    This config contains 4 parts: ACL_TABLE, ACL_RULE, POLICER, MIRROR_SESSION

    admin@vlab-01:~$ show acl table EVERFLOW_DSCP_TEST
    Name                Type         Binding    Description         Stage
    ------------------  -----------  ---------  ------------------  -------
    EVERFLOW_DSCP_TEST  MIRROR_DSCP  Ethernet0  EVERFLOW_DSCP_TEST  ingress
                                     ...

    admin@vlab-01:~$ show acl rule EVERFLOW_DSCP_TEST RULE_1
    Table               Rule      Priority  Action                                    Match
    ------------------  ------  ----------  ----------------------------------------  -------
    EVERFLOW_DSCP_TEST  RULE_1        9999  MIRROR INGRESS: mirror_session_dscp_test  DSCP: 5

    admin@vlab-01:~/everflow$ show policer everflow_static_policer
    Name                     Type    Mode         CIR       CBS
    -----------------------  ------  ------  --------  --------
    everflow_policer_test    bytes   sr_tcm  12500000  12500000

    admin@vlab-01:~$ show mirror_session mirror_session_dscp_test
    ERSPAN Sessions
    Name                      Status    SRC IP    DST IP    GRE      DSCP    TTL  Queue    Policer                  Monitor Port    SRC Port    Direction       # noqa:E501
    ------------------------  --------  --------  --------  -----  ------  -----  -------  -----------------------  --------------  ----------  -----------     # noqa:E501
    mirror_session_dscp_test  active    1.1.1.1   2.2.2.2               5     32           everflow_policer_test
    ...
    """
    table = duthost.shell("{} show acl table {}".format(ip_netns_namespace_prefix, MONITOR_CONFIG_ACL_TABLE))
    expect_res_success(duthost, table, [MONITOR_CONFIG_ACL_TABLE], [])

    rule = duthost.shell("{} show acl rule {} {}".format(ip_netns_namespace_prefix,
                                                         MONITOR_CONFIG_ACL_TABLE, MONITOR_CONFIG_ACL_RULE))
    expect_res_success(duthost, rule, [
        MONITOR_CONFIG_ACL_TABLE, MONITOR_CONFIG_ACL_RULE, MONITOR_CONFIG_MIRROR_SESSION], [])

    policer = duthost.shell("{} show policer {}".format(ip_netns_namespace_prefix, MONITOR_CONFIG_POLICER))
    expect_res_success(duthost, policer, [MONITOR_CONFIG_POLICER], [])

    mirror_session = duthost.shell("{} show mirror_session {}".format(ip_netns_namespace_prefix,
                                                                      MONITOR_CONFIG_MIRROR_SESSION))
    if is_policer_supported(duthost):
        expect_res_success(duthost, mirror_session, [
            MONITOR_CONFIG_MIRROR_SESSION, MONITOR_CONFIG_POLICER], [])
    else:
        expect_res_success(duthost, mirror_session, [MONITOR_CONFIG_MIRROR_SESSION], [])


def verify_no_monitor_config(duthost, ip_netns_namespace_prefix):
    """
    Clean up monitor config in ACL_TABLE, ACL_RULE, POLICER, MIRROR_SESSION
    """
    table = duthost.shell("{} show acl table {}".format(ip_netns_namespace_prefix, MONITOR_CONFIG_ACL_TABLE))
    expect_res_success(duthost, table, [], [MONITOR_CONFIG_ACL_TABLE])

    rule = duthost.shell("{} show acl rule {} {}".format(ip_netns_namespace_prefix,
                                                         MONITOR_CONFIG_ACL_TABLE, MONITOR_CONFIG_ACL_RULE))
    expect_res_success(duthost, rule, [], [
        MONITOR_CONFIG_ACL_TABLE, MONITOR_CONFIG_ACL_RULE, MONITOR_CONFIG_MIRROR_SESSION])

    policer = duthost.shell("{} show policer {}".format(ip_netns_namespace_prefix, MONITOR_CONFIG_POLICER))
    expect_res_success(duthost, policer, [], [MONITOR_CONFIG_POLICER])

    mirror_session = duthost.shell("{} show mirror_session {}".format(ip_netns_namespace_prefix,
                                                                      MONITOR_CONFIG_MIRROR_SESSION))
    expect_res_success(duthost, mirror_session, [], [
        MONITOR_CONFIG_MIRROR_SESSION, MONITOR_CONFIG_POLICER])


def monitor_config_add_config(duthost, enum_rand_one_frontend_asic_index, get_valid_acl_ports,
                              ip_netns_namespace_prefix):
    """ Test to add everflow always on config
    """

    namespace = duthost.get_namespace_from_asic_id(enum_rand_one_frontend_asic_index)

    # Build basic monitor session config
    mirror_session = {
        "dscp": "5",
        "dst_ip": "2.2.2.2",
        "src_ip": "1.1.1.1",
        "ttl": "32",
        "type": "ERSPAN"
    }
    if is_policer_supported(duthost):
        mirror_session["policer"] = MONITOR_CONFIG_POLICER

    json_patch = [
        {
            "op": "add",
            "path": "/ACL_TABLE/{}".format(MONITOR_CONFIG_ACL_TABLE),
            "value": {
                "policy_desc": "{}".format(MONITOR_CONFIG_ACL_TABLE),
                "ports": get_valid_acl_ports,
                "stage": "ingress",
                "type": "MIRROR_DSCP"
            }
        },
        {
            "op": "add",
            "path": "/ACL_RULE",
            "value": {
                "{}|{}".format(MONITOR_CONFIG_ACL_TABLE, MONITOR_CONFIG_ACL_RULE): {
                    "DSCP": "5",
                    "MIRROR_INGRESS_ACTION": "{}".format(MONITOR_CONFIG_MIRROR_SESSION),
                    "PRIORITY": "9999"
                }
            }
        },
        {
            "op": "add",
            "path": "/MIRROR_SESSION",
            "value": {
               "{}".format(MONITOR_CONFIG_MIRROR_SESSION): mirror_session
            }
        },
        {
            "op": "add",
            "path": "/POLICER",
            "value": {
                "{}".format(MONITOR_CONFIG_POLICER): {
                    "meter_type": "bytes",
                    "mode": "sr_tcm",
                    "cir": "12500000",
                    "cbs": "12500000",
                    "red_packet_action": "drop"
                }
            }
        }
    ]
    json_patch = format_json_patch_for_multiasic(duthost=duthost, json_data=json_patch,
                                                 is_asic_specific=True, asic_namespaces=[namespace])

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {}".format(tmpfile))

    try:
        output = apply_patch(duthost, json_data=json_patch, dest_file=tmpfile)
        expect_op_success(duthost, output)

        verify_monitor_config(duthost, ip_netns_namespace_prefix)
    finally:
        delete_tmpfile(duthost, tmpfile)


def test_monitor_config_tc1_suite(rand_selected_front_end_dut, enum_rand_one_frontend_asic_index, get_valid_acl_ports,
                                  ip_netns_namespace_prefix, cli_namespace_prefix):
    """ Test enable/disable EverflowAlwaysOn config
    """
    asic_namespace = rand_selected_front_end_dut.get_namespace_from_asic_id(enum_rand_one_frontend_asic_index)

    # Step 1: Create checkpoint at initial state where no monitor config exist
    bgp_monitor_config_cleanup(rand_selected_front_end_dut, cli_namespace_prefix, namespace=asic_namespace)
    create_checkpoint(rand_selected_front_end_dut, MONITOR_CONFIG_INITIAL_CP)

    # Step 2: Add EverflowAlwaysOn config to rand_selected_front_end_dut
    monitor_config_add_config(rand_selected_front_end_dut, enum_rand_one_frontend_asic_index, get_valid_acl_ports,
                              ip_netns_namespace_prefix)

    # Step 3: Create checkpoint that containing desired EverflowAlwaysOn config
    create_checkpoint(rand_selected_front_end_dut, MONITOR_CONFIG_TEST_CP)

    try:
        # Step 4: Rollback to initial state disabling monitor config
        output = rollback(rand_selected_front_end_dut, MONITOR_CONFIG_INITIAL_CP)
        pytest_assert(
            not output['rc'] and "Config rolled back successfull" in output['stdout'],
            "config rollback to {} failed.".format(MONITOR_CONFIG_INITIAL_CP)
        )
        verify_no_monitor_config(rand_selected_front_end_dut, ip_netns_namespace_prefix)

        # Step 5: Rollback to EverflowAlwaysOn config and verify
        output = rollback(rand_selected_front_end_dut, MONITOR_CONFIG_TEST_CP)
        pytest_assert(
            not output['rc'] and "Config rolled back successfull" in output['stdout'],
            "config rollback to {} failed.".format(MONITOR_CONFIG_TEST_CP)
        )
        verify_monitor_config(rand_selected_front_end_dut, ip_netns_namespace_prefix)

    finally:
        delete_checkpoint(rand_selected_front_end_dut, MONITOR_CONFIG_INITIAL_CP)
        delete_checkpoint(rand_selected_front_end_dut, MONITOR_CONFIG_TEST_CP)
