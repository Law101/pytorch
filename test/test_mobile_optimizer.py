import unittest
import torch
import torch.backends.xnnpack
import torch.utils.bundled_inputs
from torch.utils.mobile_optimizer import *
from torch.nn import functional as F

FileCheck = torch._C.FileCheck

class TestOptimizer(unittest.TestCase):

    @unittest.skipUnless(torch.backends.xnnpack.enabled,
                         " XNNPACK must be enabled for these tests."
                         " Please build with USE_XNNPACK=1.")
    def test_optimize_for_mobile(self):
        batch_size = 2
        input_channels_per_group = 6
        height = 16
        width = 16
        output_channels_per_group = 6
        groups = 4
        kernel_h = kernel_w = 3
        stride_h = stride_w = 1
        pad_h = pad_w = 1
        dilation = 1
        input_channels = input_channels_per_group * groups
        output_channels = output_channels_per_group * groups
        kernels = (kernel_h, kernel_w)
        strides = (stride_h, stride_w)
        paddings = (pad_h, pad_w)
        dilations = (dilation, dilation)
        conv_weight_shape = (output_channels, input_channels_per_group, kernel_h, kernel_w)
        conv_bias_shape = (output_channels)

        input_data = torch.rand((batch_size, input_channels, height, width))
        conv_weight = torch.rand((output_channels, input_channels_per_group, kernel_h, kernel_w))
        conv_bias = torch.rand((output_channels))
        result = F.conv2d(input_data, conv_weight, conv_bias, strides, paddings, dilations, groups)
        weight_output_dim = 24
        linear_input_shape = result.shape[1]
        linear_weight_shape = (weight_output_dim, linear_input_shape)

        class MyTestModule(torch.nn.Module):
            def __init__(self):
                super(MyTestModule, self).__init__()
                self.conv_weight = torch.nn.Parameter(torch.Tensor(torch.rand(conv_weight_shape)))
                self.conv_bias = torch.nn.Parameter(torch.Tensor(torch.rand((conv_bias_shape))))
                self.linear_weight = torch.nn.Parameter(torch.Tensor(torch.rand(linear_weight_shape)))
                self.linear_bias = torch.nn.Parameter(torch.Tensor(torch.rand((weight_output_dim))))
                self.strides = strides
                self.paddings = paddings
                self.dilations = dilations
                self.groups = groups

            def forward(self, x):
                o = F.conv2d(x, self.conv_weight, self.conv_bias,
                             self.strides, self.paddings, self.dilations, self.groups)
                o = F.relu(o)
                o = o.permute([0, 2, 3, 1])
                o = F.linear(o, self.linear_weight, self.linear_bias)
                return F.relu(o)

        data_shape = (batch_size, input_channels, height, width)
        input_data = torch.normal(1, 20, size=data_shape)

        scripted_model = torch.jit.script(MyTestModule())
        scripted_model.eval()
        initial_result = scripted_model(input_data)

        optimized_scripted_model = optimize_for_mobile(scripted_model)
        optimized_result = optimized_scripted_model(input_data)

        FileCheck().check_not("Tensor = aten::conv2d") \
                   .check_not("Tensor = prim::CallFunction") \
                   .check_not("prepacked::conv2d_clamp_prepack") \
                   .check_count("prepacked::conv2d_clamp_run", 1, exactly=True) \
                   .check_not("prepacked::linear_clamp_prepack") \
                   .check_count("prepacked::linear_clamp_run", 1, exactly=True) \
                   .run(optimized_scripted_model.graph)

        torch.testing.assert_allclose(initial_result, optimized_result, rtol=1e-2, atol=1e-3)

    def test_generate_mobile_module_lints(self):
        class MyTestModule(torch.nn.Module):
            def __init__(self):
                super(MyTestModule, self).__init__()
                self.fc = torch.nn.Linear(4, 4)
                self.dropout = torch.nn.Dropout(p=0.5)

            def forward(self, inputs):
                out = self.fc(inputs)
                out = self.dropout(out)
                return out

        class MyBNModule(torch.nn.Module):
            def __init__(self):
                super(MyBNModule, self).__init__()
                self.bn = torch.nn.BatchNorm2d(4, affine=True)

            def forward(self, inputs):
                bn = self.bn(inputs)
                return bn

        class MyBundledInputModule(torch.nn.Module):
            def __init__(self):
                super(MyBundledInputModule, self).__init__()

            def forward(self, inputs):
                return inputs

        def get_lint_count_by_type(lint_type, module_lint_List):
            return len([lint_dict for lint_dict in module_lint_List if lint_dict['name'] == lint_type.name])

        test_module = torch.jit.script(MyTestModule())
        test_module_lint_list = generate_mobile_module_lints(test_module)
        self.assertEqual(len(test_module_lint_list), 4)
        self.assertEqual(get_lint_count_by_type(LintCode.BUNDLED_INPUT, test_module_lint_list), 1)
        self.assertEqual(get_lint_count_by_type(LintCode.DROPOUT, test_module_lint_list), 1)
        self.assertEqual(get_lint_count_by_type(LintCode.REQUIRES_GRAD, test_module_lint_list), 2)

        bn_module = torch.jit.script(MyBNModule())
        bn_module_lint_list = generate_mobile_module_lints(bn_module)
        self.assertEqual(len(bn_module_lint_list), 4)
        self.assertEqual(get_lint_count_by_type(LintCode.BUNDLED_INPUT, bn_module_lint_list), 1)
        self.assertEqual(get_lint_count_by_type(LintCode.BATCHNORM, bn_module_lint_list), 1)
        self.assertEqual(get_lint_count_by_type(LintCode.REQUIRES_GRAD, bn_module_lint_list), 2)

        bi_module = torch.jit.script(MyBundledInputModule())
        torch.utils.bundled_inputs.augment_model_with_bundled_inputs(
            bi_module, [(torch.tensor([1]),)], [])
        bi_module_lint_list = generate_mobile_module_lints(bi_module)
        self.assertEqual(len(bi_module_lint_list), 0)

if __name__ == '__main__':
    unittest.main()
